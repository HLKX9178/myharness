"""权限覆盖测试。

回归目标：重构之前，`read_file` 和 `run_glob` **完全绕过**了策略检查 ——
授权是否生效，取决于每个工具作者有没有记得调用 `safe_path`。

现在权限判定位于 dispatch 路径上（security.permission_hook），
任何走 execute_tool() 的工具都绕不过去。下面的测试锁住这一点。
"""

from pathlib import Path
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

# 必须拦下的命令。覆盖的是「同一件事的不同写法」——
# 只写死一种拼写（比如只拦 "rm -rf /"）等于没拦。
DANGEROUS_COMMANDS = [
    "rm -rf /",
    "rm -rf /*",
    "rm -fr /",
    "rm -Rf /",
    "rm --recursive --force /",
    "sudo rm -rf /",           # 包装命令不得成为绕过路径
    "nohup rm -rf / &",
    "rm -rf ~",                # 家目录同样是灾难
    "rm -rf $HOME",
    "$(rm -rf /)",             # 命令替换
    "mkfs.ext4 /dev/sda",
    "mkfs.xfs /dev/sdb",
    "dd if=/dev/zero of=/dev/sda",
    "dd of=/dev/nvme0n1 if=/dev/zero",
    "chmod 777 /etc/passwd",
    "chmod -R 777 x",
    "chmod 0777 x",
    "chmod a+rwx x",
    "cat x > /dev/sda",        # 重定向写块设备
    "echo x >> /dev/mem",
    "parted /dev/sda mklabel gpt",
    "shutdown -h now; format C:",
    r"C:\Windows\System32\format.com C:",
    ":(){ :|:& };:",           # fork bomb
]


@pytest.mark.parametrize("command", DANGEROUS_COMMANDS)
def test_dangerous_bash_command_denied(command):
    executed = []

    out = dispatch.execute_tool(
        make_block("bash", command=command),
        {"bash": record_calls(executed)},
    )

    assert "Permission denied" in out
    assert executed == []


# 必须放行的命令。每一条都对应一个之前会被误拒的真实写法 ——
# 旧实现用子串匹配 "format" / "chmod 777" / "> /dev/"，这几条全部中招。
SAFE_COMMANDS = [
    'printf "format"',
    "black --format src/",
    "python -c \"print('{:.1f}'.format(1))\"",
    'echo chmod 777',
    "echo hi > /dev/null",     # /dev/null 是伪设备，写入无害
    "cmd > /dev/null 2>&1",
    "chmod +x deploy.sh",      # 给脚本加执行位是日常操作
    "rm -rf ./build",          # 清构建产物：目标是相对目录，不是根
    "rm -rf .pytest_cache",
    "rm -rf build dist *.log",
    "dd if=/dev/zero of=./disk.img bs=1M count=10",
    'find . -name "*.py" -exec rm -f {} ;',
    'git commit -m "fix format string"',
    "pip install reformat",
    "npm run build && rm -rf dist",
    "grep -rn \"format\" .",
]


@pytest.mark.parametrize("command", SAFE_COMMANDS)
def test_legitimate_bash_command_allowed(command):
    executed = []

    out = dispatch.execute_tool(
        make_block("bash", command=command),
        {"bash": record_calls(executed)},
    )

    assert out == "handler-ran", f"合法命令被误拒: {command}"
    assert len(executed) == 1


def test_safe_bash_command_allowed():
    out = dispatch.execute_tool(
        make_block("bash", command="echo hello"),
        {"bash": lambda **kw: "hello"},
    )
    assert out == "hello"


# ============================================================
# 命令检查：token 判定而非子串判定
# ============================================================

def test_format_as_argument_is_not_a_format_command():
    """"format" 出现在参数或字符串里不是危险操作。

    回归目标：旧实现用 `if "format" in command`，于是格式化字符串、
    `black --format`、`pip install reformat` 全被拒。
    """
    from myharness.security import SecurityPolicy

    policy = SecurityPolicy(Path("."))

    for command in ('printf "format"', "black --format src/", "pip install reformat"):
        allowed, reason = policy.check_command(command)
        assert allowed, f"{command} 被误拒: {reason}"


def test_format_at_command_position_is_denied():
    """同名 token 出现在命令名位置就必须拦 —— 这才是要挡的那件事。"""
    from myharness.security import SecurityPolicy

    policy = SecurityPolicy(Path("."))

    assert not policy.check_command("format C:")[0]
    assert not policy.check_command("shutdown -h now; format C:")[0]
    assert not policy.check_command(r"C:\Windows\System32\format.com C:")[0]
    # 子 shell 里的命令名同样要认出来
    assert not policy.check_command('bash -c "format C:"')[0]


def test_chmod_all_permissions_denied_but_plus_x_allowed():
    """判的是权限位，不是字面串。

    回归目标：旧实现只拦 "chmod 777" 与 "chmod -R 777" 两个字面串，
    于是 `chmod 0777`、`chmod a+rwx` 能绕过，而 `echo chmod 777` 被误拒。
    """
    from myharness.security import SecurityPolicy

    policy = SecurityPolicy(Path("."))

    for command in ("chmod 0777 x", "chmod a+rwx x", "chmod -R 777 x"):
        assert not policy.check_command(command)[0], f"{command} 应被拒绝"

    for command in ("chmod +x deploy.sh", "chmod 755 x", "echo chmod 777"):
        allowed, reason = policy.check_command(command)
        assert allowed, f"{command} 被误拒: {reason}"


def test_dev_null_redirect_allowed_but_real_device_denied():
    """`> /dev/null` 极常用，`> /dev/sda` 极危险 —— 两者必须分开。"""
    from myharness.security import SecurityPolicy

    policy = SecurityPolicy(Path("."))

    assert policy.check_command("echo hi > /dev/null")[0]
    assert policy.check_command("cmd > /dev/null 2>&1")[0]
    assert not policy.check_command("cat x > /dev/sda")[0]


def test_wrapper_commands_do_not_hide_dangerous_ones():
    """`sudo` / `nohup` / `env` 只是包装，不改变指令本体。"""
    from myharness.security import SecurityPolicy

    policy = SecurityPolicy(Path("."))

    for command in (
        "sudo rm -rf /",
        "nohup rm -rf / &",
        "env FOO=bar rm -rf /",
        "sudo -u root rm -rf /",
    ):
        allowed, reason = policy.check_command(command)
        assert not allowed, f"{command} 应被拒绝（漏检了）: {reason}"

    # 包装本身不构成危险
    assert policy.check_command("sudo systemctl restart nginx")[0]


def test_nested_shell_command_is_rechecked():
    """子 shell 里的命令要当成新命令重新检查。

    回归目标：旧实现靠子串匹配「顺带」命中 `sh -c "rm -rf /"`。改成 token
    判定后必须显式补回这一层，否则嵌套写法会成为绕过路径。
    """
    from myharness.security import SecurityPolicy

    policy = SecurityPolicy(Path("."))

    for command in (
        'sh -c "rm -rf /"',
        "bash -c 'rm -rf /'",
        "env X=1 sh -c 'rm -rf /'",
        'eval "rm -rf /"',
    ):
        allowed, reason = policy.check_command(command)
        assert not allowed, f"嵌套命令漏检: {command} -> {reason}"

    # 嵌套的无害命令照旧放行
    assert policy.check_command('sh -c "echo hi"')[0]
    assert policy.check_command('bash -c "chmod +x a.sh"')[0]


# ============================================================
# 审计：允许与拒绝都必须留痕（M2 验收标准）
# ============================================================

def test_denied_call_counts_as_tool_failure():
    """拒绝结果必须带 "Error: " 前缀（契约 1）。

    回归目标：之前返回的是 "Permission denied: ..."，而
    ConsecutiveFailureGuard 只认 "Error:" 开头 —— 于是模型反复越界调用
    也不会累计失败次数，告警和中止永远不会触发。
    """
    out = dispatch.execute_tool(
        make_block("read_file", path="../../etc/passwd"),
        {"read_file": record_calls([])},
    )

    assert out.startswith("Error:")
    assert "Permission denied" in out


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
