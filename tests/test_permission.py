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
from myharness.security import init_policy, permission_hook

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
