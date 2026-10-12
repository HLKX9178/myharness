"""权限覆盖测试。

回归目标：重构之前，`read_file` 和 `run_glob` **完全绕过**了策略检查 ——
授权是否生效，取决于每个工具作者有没有记得调用 `safe_path`。

现在权限判定位于 dispatch 路径上（security.permission_hook），
任何走 execute_tool() 的工具都绕不过去。下面的测试锁住这一点。
"""

from types import SimpleNamespace

import pytest

from myharness import dispatch, hooks
from myharness.audit import get_audit_logger, init_audit_logger
from myharness.security import get_policy, init_policy, permission_hook

PATH_TOOLS = ["read_file", "write_file", "edit_file"]


def make_block(name: str, **inputs):
    return SimpleNamespace(name=name, input=inputs)


@pytest.fixture(autouse=True)
def isolated_policy(tmp_path):
    """每个用例使用独立的工作区与审计日志。"""
    hooks.clear_hooks()
    init_policy(tmp_path)
    init_audit_logger(tmp_path / "audit.jsonl")
    hooks.register_hook("PreToolUse", permission_hook)
    yield
    hooks.clear_hooks()


def record_calls(sink: list):
    """返回一个 handler，记录自己被调用（正常情况不应被调用）。"""
    def handler(**kwargs):
        sink.append(kwargs)
        return "handler-ran"
    return handler


# ============================================================
# 路径类工具：全覆盖
# ============================================================

@pytest.mark.parametrize("tool", PATH_TOOLS)
def test_path_tool_denied_outside_workspace(tool):
    executed = []

    out = dispatch.execute_tool(
        make_block(tool, path="../../outside.txt"),
        {tool: record_calls(executed)},
    )

    assert "Permission denied" in out
    assert executed == [], "越界调用不得到达 handler"


@pytest.mark.parametrize("tool", PATH_TOOLS)
def test_path_tool_allowed_inside_workspace(tool):
    executed = []

    out = dispatch.execute_tool(
        make_block(tool, path="inside.txt"),
        {tool: record_calls(executed)},
    )

    assert out == "handler-ran"
    assert len(executed) == 1


def test_read_file_is_now_covered():
    """这是本次重构修掉的具体漏洞。"""
    executed = []

    out = dispatch.execute_tool(
        make_block("read_file", path="../../../etc/passwd"),
        {"read_file": record_calls(executed)},
    )

    assert "Permission denied" in out
    assert executed == []


def test_absolute_path_outside_workspace_denied():
    executed = []

    out = dispatch.execute_tool(
        make_block("read_file", path="C:/Windows/System32/drivers/etc/hosts"),
        {"read_file": record_calls(executed)},
    )

    assert "Permission denied" in out
    assert executed == []


# ============================================================
# 工作区切换：工具边界与权限边界必须一致
# ============================================================

@pytest.fixture
def other_workspace(tmp_path):
    """另一个（不存在的）工作区目录。"""
    path = tmp_path / "agent_ws"
    path.mkdir()
    return path


def test_use_workdir_switches_both_layers(other_workspace):
    """use_workdir 必须同时切工具边界与权限边界。

    回归目标：工具层改成动态读取 config.WORKDIR 后，权限层仍固定在
    import 时的工作区，两层就会分叉 —— 审计记录 A，文件实际写到 B。
    """
    from myharness import config
    from myharness.tools import safe_path

    with config.use_workdir(other_workspace):
        # 工具层跟着切
        assert safe_path("a.txt") == (other_workspace / "a.txt").resolve()
        # 权限层跟着切
        assert get_policy().workspace == other_workspace.resolve()

        # 绝对路径落在当前工作区内 -> 必须放行（分叉时这里会被拒）
        assert permission_hook(
            make_block("write_file", path=str(other_workspace / "a.txt"))
        ) is None

    # 退出后两层必须仍然指向同一个工作区
    assert config.get_workdir() == get_policy().workspace


def test_use_workdir_restores_after_exception(other_workspace):
    """with 块内抛异常也必须恢复，否则后续操作会全部写错地方。"""
    from myharness import config

    before = config.get_workdir()

    with pytest.raises(RuntimeError):
        with config.use_workdir(other_workspace):
            assert config.get_workdir() == other_workspace.resolve()
            raise RuntimeError("boom")

    assert config.get_workdir() == before
    assert get_policy().workspace == before


def test_use_workdir_rejects_missing_directory(tmp_path):
    """目录不存在时直接报错，不静默接受一个写不进去的工作区。"""
    from myharness import config

    with pytest.raises(ValueError, match="existing directory"):
        with config.use_workdir(tmp_path / "missing"):
            pass


def test_audit_target_matches_real_location(other_workspace):
    """审计记录的 target 必须是实际操作的那个路径。

    两层分叉时，审计会记成旧工作区下的路径 —— M2 验收项
    「能回答某次操作是谁在何时批准/拒绝的」就不再可靠。
    """
    from myharness import config

    with config.use_workdir(other_workspace):
        permission_hook(make_block("write_file", path="note.txt"))
        record = get_audit_logger().query_history(limit=1)[0]

    assert record["target"] == str(other_workspace / "note.txt")


# ============================================================
# 命令类工具
# ============================================================

@pytest.mark.parametrize("command", [
    "rm -rf /",
    "mkfs.ext4 /dev/sda",
    "dd if=/dev/zero of=/dev/sda",
    "chmod 777 /etc/passwd",
])
def test_dangerous_bash_command_denied(command):
    executed = []

    out = dispatch.execute_tool(
        make_block("bash", command=command),
        {"bash": record_calls(executed)},
    )

    assert "Permission denied" in out
    assert executed == []


def test_safe_bash_command_allowed():
    out = dispatch.execute_tool(
        make_block("bash", command="echo hello"),
        {"bash": lambda **kw: "hello"},
    )
    assert out == "hello"


# ============================================================
# 审计：允许与拒绝都必须留痕（M2 验收标准）
# ============================================================

def test_denied_call_is_audited():
    dispatch.execute_tool(
        make_block("read_file", path="../../etc/passwd"),
        {"read_file": lambda **kw: "x"},
    )

    records = get_audit_logger().query_history()
    assert any(r["decision"] == "denied" for r in records)


def test_allowed_call_is_audited():
    dispatch.execute_tool(
        make_block("read_file", path="ok.txt"),
        {"read_file": lambda **kw: "ok"},
    )

    records = get_audit_logger().query_history()
    assert any(r["decision"] == "allowed" for r in records)


def test_audit_record_contains_reason_and_action():
    dispatch.execute_tool(
        make_block("bash", command="rm -rf /"),
        {"bash": lambda **kw: "x"},
    )

    records = get_audit_logger().query_history()
    denied = [r for r in records if r["decision"] == "denied"]

    assert denied, "拒绝的操作必须被记录"
    assert denied[0]["action"] == "bash"
    assert denied[0]["reason"]
    assert denied[0]["timestamp"]
