"""工具调用守卫的测试。

覆盖两个从 agent_loop 迁移到 hook 的策略：

  RepeatCallGuard          重复调用拦截
  ConsecutiveFailureGuard  连续失败统计与中止判定

（第三个守卫 ProgressGuard 的用例在 test_progress_guard.py。）

并锁住一个协议约束：被拦截的调用必须返回 tool_result 文本，
而不是「跳过执行 + 追加纯文本 user 消息」—— 后者会破坏
tool_use / tool_result 的配对（契约 2）。
"""

from types import SimpleNamespace

import pytest

from myharness import dispatch, hooks
from myharness.bootstrap import install_default_hooks
from myharness.guards import (
    ConsecutiveFailureGuard,
    RepeatCallGuard,
    failure_guard,
    progress_guard,
    repeat_call_guard,
    reset_turn_state,
)


def make_block(name: str, **inputs):
    return SimpleNamespace(name=name, input=inputs)


@pytest.fixture(autouse=True)
def clean_hooks():
    """每个用例前后清空注册表与守卫状态。"""
    hooks.clear_hooks()
    reset_turn_state()
    yield
    hooks.clear_hooks()
    reset_turn_state()


# ============================================================
# RepeatCallGuard
# ============================================================

def test_first_call_passes():
    assert RepeatCallGuard()(make_block("read_file", path="a.txt")) is None


def test_identical_consecutive_call_is_blocked():
    guard = RepeatCallGuard()
    block = make_block("read_file", path="a.txt")

    assert guard(block) is None
    result = guard(block)

    assert result is not None
    assert "相同参数" in result


def test_different_args_are_not_blocked():
    guard = RepeatCallGuard()

    assert guard(make_block("read_file", path="a.txt")) is None
    assert guard(make_block("read_file", path="b.txt")) is None


def test_same_args_different_tool_not_blocked():
    guard = RepeatCallGuard()

    assert guard(make_block("read_file", path="a.txt")) is None
    assert guard(make_block("write_file", path="a.txt")) is None


def test_blocks_only_once():
    """拦截一次后必须放行，否则模型永远无法重试。"""
    guard = RepeatCallGuard()
    block = make_block("read_file", path="a.txt")

    guard(block)
    assert guard(block) is not None
    assert guard(block) is None


def test_arg_order_does_not_matter():
    guard = RepeatCallGuard()

    assert guard(make_block("edit_file", path="a", old="x", new="y")) is None
    assert guard(make_block("edit_file", new="y", old="x", path="a")) is not None


def test_reset_clears_state():
    guard = RepeatCallGuard()
    block = make_block("read_file", path="a.txt")

    guard(block)
    guard.reset()

    assert guard(block) is None


# ============================================================
# 协议约束：拦截必须作为 tool_result 返回
# ============================================================

def test_blocked_repeat_returns_tool_result_text():
    """回归测试：原实现会破坏 tool_use / tool_result 配对。"""
    hooks.register_hook("PreToolUse", RepeatCallGuard())

    handlers = {"read_file": lambda **kw: "content"}
    block = make_block("read_file", path="a.txt")

    first = dispatch.execute_tool(block, handlers)
    second = dispatch.execute_tool(block, handlers)

    assert first == "content"
    assert isinstance(second, str), "拦截结果必须是字符串，才能作为 tool_result 回填"
    assert "相同参数" in second


# ============================================================
# ConsecutiveFailureGuard
# ============================================================

def test_counter_increments_on_error():
    guard = ConsecutiveFailureGuard(limit=3)

    guard(make_block("bash"), "Error: boom")
    assert guard.failures == 1

    guard(make_block("bash"), "Error: boom again")
    assert guard.failures == 2


def test_success_resets_counter():
    guard = ConsecutiveFailureGuard(limit=3)

    guard(make_block("bash"), "Error: boom")
    guard(make_block("bash"), "Error: boom")
    guard(make_block("bash"), "ok")

    assert guard.failures == 0
    assert not guard.should_abort


def test_should_abort_at_limit():
    guard = ConsecutiveFailureGuard(limit=3)

    for _ in range(2):
        guard(make_block("bash"), "Error: x")
    assert not guard.should_abort

    guard(make_block("bash"), "Error: x")
    assert guard.should_abort


def test_feedback_emitted_once_then_cleared():
    guard = ConsecutiveFailureGuard(limit=3, warn_after=2)

    guard(make_block("bash"), "Error: x")
    assert guard.take_feedback() is None

    guard(make_block("bash"), "Error: x")
    assert guard.take_feedback() is not None
    assert guard.take_feedback() is None, "反馈只能取一次"


def test_feedback_cleared_by_success():
    guard = ConsecutiveFailureGuard(limit=5, warn_after=2)

    guard(make_block("bash"), "Error: x")
    guard(make_block("bash"), "Error: x")
    guard(make_block("bash"), "ok")

    assert guard.take_feedback() is None


def test_reset_clears_everything():
    guard = ConsecutiveFailureGuard(limit=3)
    guard(make_block("bash"), "Error: x")
    guard(make_block("bash"), "Error: x")

    guard.reset()

    assert guard.failures == 0
    assert guard.take_feedback() is None


# ============================================================
# 装配与跨轮重置
# ============================================================

def test_default_guards_are_registered():
    """守卫必须被装到正确的扩展点上。"""
    install_default_hooks()

    assert repeat_call_guard in hooks.registered_hooks("PreToolUse")
    assert failure_guard in hooks.registered_hooks("PostToolUse")
    assert progress_guard in hooks.registered_hooks("PostToolUse")


def test_permission_runs_before_repeat_guard():
    """被权限拒绝的调用不应被记为"已调用过"。"""
    install_default_hooks()

    names = [
        getattr(f, "__name__", type(f).__name__)
        for f in hooks.registered_hooks("PreToolUse")
    ]
    assert names.index("permission_hook") < names.index("RepeatCallGuard")


def test_install_is_idempotent():
    install_default_hooks()
    install_default_hooks()

    pre = hooks.registered_hooks("PreToolUse")
    assert pre.count(repeat_call_guard) == 1, "重复装配不得产生重复 hook"


def test_reset_turn_state_clears_all_guards():
    repeat_call_guard(make_block("read_file", path="a.txt"))
    failure_guard(make_block("bash"), "Error: x")
    progress_guard(make_block("read_file", path="a.txt"), "固定内容")

    reset_turn_state()

    assert repeat_call_guard(make_block("read_file", path="a.txt")) is None
    assert failure_guard.failures == 0
    assert progress_guard.take_feedback() is None
    assert not progress_guard.should_abort


def test_post_tool_use_hooks_all_receive_events():
    """PostToolUse 不短路：一个 hook 的返回值不得吞掉其它 hook 的事件。"""
    received = []

    hooks.register_hook("PostToolUse", lambda b, o: "ignored-return")
    hooks.register_hook("PostToolUse", lambda b, o: received.append(o))

    dispatch.execute_tool(
        make_block("echo", text="hi"),
        {"echo": lambda **kw: "hi"},
    )

    assert received == ["hi"], "第二个 hook 也必须收到事件"
