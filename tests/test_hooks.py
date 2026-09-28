"""Hook 机制与工具调度的测试（对应 AGENTS.md 契约 6）。

这些测试的目的不是"验证功能"，而是**锁住架构约束**：

  - 横切逻辑只能挂在 hook 上
  - 工具执行只能经过 dispatch.execute_tool()
  - 被拦截的工具绝不允许到达 handler

一旦有人把逻辑写回 agent_loop，或绕过 execute_tool 直接调 handler，
这里的测试就会失败。
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from myharness import dispatch, hooks

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_block(name: str, **inputs):
    """构造一个最小的 tool_use block 替身。"""
    return SimpleNamespace(name=name, input=inputs)


@pytest.fixture(autouse=True)
def clean_hooks():
    """每个用例前后清空 hook 注册表，避免相互污染。"""
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


# ============================================================
# 注册表语义
# ============================================================

def test_unknown_event_is_rejected():
    with pytest.raises(ValueError):
        hooks.register_hook("NotAnEvent", lambda: None)


def test_all_hooks_passing_returns_none():
    hooks.register_hook("PreToolUse", lambda b: None)
    hooks.register_hook("PreToolUse", lambda b: None)
    assert hooks.trigger_hooks("PreToolUse", make_block("x")) is None


def test_trigger_short_circuits_on_first_non_none():
    order = []

    def first(block):
        order.append("first")
        return "blocked"

    def second(block):
        order.append("second")
        return None

    hooks.register_hook("PreToolUse", first)
    hooks.register_hook("PreToolUse", second)

    assert hooks.trigger_hooks("PreToolUse", make_block("x")) == "blocked"
    assert order == ["first"], "拦截后不应继续执行后续 hook"


# ============================================================
# dispatch 语义
# ============================================================

def test_blocked_tool_never_reaches_handler():
    """被 PreToolUse 拦截的工具，handler 必须完全不执行。"""
    executed = []
    hooks.register_hook("PreToolUse", lambda b: "Permission denied")

    out = dispatch.execute_tool(
        make_block("write_file", path="a.txt"),
        {"write_file": lambda **kw: executed.append(kw) or "written"},
    )

    assert out == "Permission denied"
    assert executed == []


def test_posttooluse_receives_handler_output():
    seen = []
    hooks.register_hook("PostToolUse", lambda b, o: seen.append(o))

    out = dispatch.execute_tool(
        make_block("echo", text="hi"),
        {"echo": lambda **kw: "hi"},
    )

    assert out == "hi"
    assert seen == ["hi"]


def test_unknown_tool_returns_string_not_exception():
    out = dispatch.execute_tool(make_block("nope"), {})
    assert isinstance(out, str)
    assert "Unknown tool" in out


def test_handler_exception_becomes_error_string():
    """契约 4：错误是数据，不是异常。"""
    def boom(**kwargs):
        raise ValueError("kaboom")

    out = dispatch.execute_tool(make_block("boom"), {"boom": boom})

    assert isinstance(out, str)
    assert out.startswith("Error:")
    assert "kaboom" in out


def test_missing_input_does_not_crash_dispatch():
    """模型漏传参数时，应返回错误字符串而不是抛异常。"""
    def needs_arg(required: str) -> str:
        return required

    out = dispatch.execute_tool(make_block("needs_arg"), {"needs_arg": needs_arg})

    assert out.startswith("Error:")


# ============================================================
# 架构约束（回归测试）
# ============================================================

def test_agent_loop_goes_through_execute_tool():
    """Loop 不得直接查 handler 表，必须经过 execute_tool。"""
    source = (REPO_ROOT / "myharness" / "loop.py").read_text(encoding="utf-8")

    assert "execute_tool(" in source, "loop 必须调用 execute_tool"
    assert "TOOL_HANDLERS" not in source, "loop 不得直接访问 handler 表"


def test_agent_loop_uses_assemble_tool_pool():
    """工具池必须每轮组装，为 M4 的 MCP 动态工具预留扩展点。"""
    source = (REPO_ROOT / "myharness" / "loop.py").read_text(encoding="utf-8")

    assert "assemble_tool_pool(" in source


def test_execute_tool_is_the_only_dispatch_entry():
    """dispatch 模块应只暴露一个执行入口。"""
    public = [
        name for name in dir(dispatch)
        if not name.startswith("_") and callable(getattr(dispatch, name))
        and getattr(getattr(dispatch, name), "__module__", None) == dispatch.__name__
    ]
    assert public == ["execute_tool"]
