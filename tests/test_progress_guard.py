"""无进展检测（ProgressGuard）与循环终止原因（LoopOutcome）。

对应 ROADMAP §9.4 / §9.5 的验收标准。核心命题有两条：

  1. 轮次是**动作数**，不是进度 —— 所以 MAX_TURNS 可以调得很大，
     前提是先有一层看结果的刹车；
  2. 「被截断」不允许被当成「完成」 —— 终止原因必须可辨。

单测覆盖 guard 自身（1-8），集成覆盖 agent_loop 的三个出口（9-11）。
"""

from types import SimpleNamespace

import pytest

from myharness import guards, hooks, loop
from myharness.bootstrap import install_default_hooks
from myharness.guards import (
    ConsecutiveFailureGuard,
    ProgressGuard,
    failure_guard,
    progress_guard,
    reset_turn_state,
)
from myharness.providers.base import ProviderError


def make_block(name: str = "read_file", **inputs):
    return SimpleNamespace(name=name, input=inputs)


@pytest.fixture(autouse=True)
def clean_state():
    hooks.clear_hooks()
    reset_turn_state()
    yield
    hooks.clear_hooks()
    reset_turn_state()


# ============================================================
# ProgressGuard 单测
# ============================================================

def test_warns_after_three_identical_results():
    """验收 1：连续 3 次结果一致 → 反馈非空，且含工具名与次数。"""
    guard = ProgressGuard()

    for _ in range(3):
        guard(make_block(path="a.txt"), "固定内容")

    feedback = guard.take_feedback()

    assert feedback is not None
    assert "read_file" in feedback, "反馈必须点名是哪个工具在打转"
    assert "3 次" in feedback, "反馈必须给出模型自己数不出来的次数"
    assert "固定内容" not in feedback, "反馈不该复述结果本身"
    assert not guard.should_abort


def test_aborts_after_five_identical_results():
    """验收 2：连续 5 次结果一致 → should_abort。"""
    guard = ProgressGuard()

    for _ in range(4):
        guard(make_block(path="a.txt"), "固定内容")

    assert not guard.should_abort

    guard(make_block(path="a.txt"), "固定内容")

    assert guard.should_abort
    assert "read_file" in guard.detail and "5 次" in guard.detail


def test_polling_is_not_no_progress():
    """验收 3：参数相同但结果每次不同（轮询）→ 不警告、不终止。"""
    guard = ProgressGuard()

    for i in range(20):
        guard(make_block("get_task_result", task_id="t1"), f"running {i}")

    assert guard.take_feedback() is None
    assert not guard.should_abort


def test_alternating_calls_still_accumulate():
    """验收 4：A/B/A/B 交替重复也累计 —— 旧守卫抓不到的场景。

    只看「上一次调用」的守卫会被 B 打断，永远看不到 A 重复了三次；
    ProgressGuard 按指纹各自记账，交替也不影响。
    """
    guard = ProgressGuard()
    a = make_block("read_file", path="a.txt")
    b = make_block("read_file", path="b.txt")

    guard(a, "A 的内容")
    guard(b, "B 的内容")
    guard(a, "A 的内容")
    guard(b, "B 的内容")
    guard(a, "A 的内容")

    feedback = guard.take_feedback()
    assert feedback is not None
    assert '"a.txt"' in feedback, "被点名的应该是 A 那一次调用"
    assert not guard.should_abort


def test_different_params_are_tracked_separately():
    """验收 5：同工具不同参数互不影响。"""
    guard = ProgressGuard()
    a = make_block("read_file", path="a.txt")
    b = make_block("read_file", path="b.txt")

    for _ in range(2):
        guard(a, "同样的内容")
        guard(b, "同样的内容")

    assert guard.take_feedback() is None, "各自只有 2 次，都不该警告"

    guard(a, "同样的内容")

    assert guard.take_feedback() is not None, "A 到第 3 次才该警告"


def test_key_order_does_not_change_fingerprint():
    """验收 6：参数键序不同、语义相同 → 同一指纹。"""
    guard = ProgressGuard()

    guard(make_block("edit_file", path="a.py", old="x", new="y"), "ok")
    guard(make_block("edit_file", new="y", old="x", path="a.py"), "ok")

    assert guard.take_feedback() is None

    guard(make_block("edit_file", path="a.py", old="x", new="y"), "ok")

    assert guard.take_feedback() is not None


def test_nested_params_are_canonicalized():
    """嵌套 dict 也要稳定：``str(sorted(items))`` 在这里会算错。"""
    guard = ProgressGuard()

    for _ in range(3):
        guard(
            make_block("bash", env={"B": 2, "A": 1}, cmd="ls"),
            "output",
        )

    assert guard.take_feedback() is not None


def test_reset_clears_state():
    """验收 7：reset() 后一切归零。"""
    guard = ProgressGuard()

    for _ in range(5):
        guard(make_block(path="a.txt"), "固定内容")

    assert guard.should_abort

    guard.reset()

    assert not guard.should_abort
    assert guard.take_feedback() is None
    assert guard.detail == ""

    guard(make_block(path="a.txt"), "固定内容")
    assert guard.take_feedback() is None, "重新开始只算 1 次"


def test_failure_guard_and_progress_guard_count_independently():
    """验收 8：两者看同一批事件，各按各的规则计数，不互相污染。

    连续失败 3 次时 failure_guard 已经中止，而 ProgressGuard 只是刚开始
    提醒（3 < 5）—— 说明它们不是同一个计数器。
    """
    failure = ConsecutiveFailureGuard(limit=3, warn_after=2)
    progress = ProgressGuard()
    block = make_block("bash", cmd="make")

    for _ in range(3):
        failure(block, "Error: boom")
        progress(block, "Error: boom")

    assert failure.should_abort
    assert not progress.should_abort
    assert progress.take_feedback() is not None


# ============================================================
# 装配
# ============================================================

def test_progress_guard_is_registered_on_post_tool_use():
    install_default_hooks()

    assert progress_guard in hooks.registered_hooks("PostToolUse")
    assert failure_guard in hooks.registered_hooks("PostToolUse")


def test_reset_turn_state_clears_progress_guard():
    progress_guard(make_block(path="a.txt"), "固定内容")
    progress_guard(make_block(path="a.txt"), "固定内容")
    progress_guard(make_block(path="a.txt"), "固定内容")

    assert progress_guard.take_feedback() is not None

    reset_turn_state()

    assert progress_guard.take_feedback() is None
    assert not progress_guard.should_abort


# ============================================================
# 集成：agent_loop 的出口
# ============================================================

def _tool_use(tool_id: str, name: str = "read_file", args: dict | None = None):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args or {})


def _text(text: str):
    return SimpleNamespace(type="text", text=text)


def _response(content: list, input_tokens: int = 100, output_tokens: int = 100):
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


class _StuckProvider:
    """永远发起同一个工具调用 —— 模拟「原地打转」。"""

    def __init__(self, name: str = "read_file", args: dict | None = None):
        self.name = name
        self.args = args or {"path": "a.txt"}
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        return _response([_tool_use(f"call_{self.calls}", self.name, dict(self.args))])


def test_loop_stops_on_no_progress_before_max_turns(monkeypatch, tmp_path):
    """验收 9（最重要）：轮次调大之后，原地打转仍然会被截住。

    RepeatCallGuard 会隔一次拦一次，所以五条相同结果需要九轮才攒够 ——
    正是这种「有噪声的重复」才需要结果指纹，只看相邻调用会漏掉。
    """
    (tmp_path / "a.txt").write_text("固定内容", encoding="utf-8")

    hooks.register_hook("PreToolUse", guards.repeat_call_guard)
    hooks.register_hook("PostToolUse", guards.progress_guard)
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10**9)
    monkeypatch.setattr(loop, "provider", _StuckProvider())

    messages = [{"role": "user", "content": "一直读同一个文件"}]
    outcome = loop.agent_loop(messages, workspace=tmp_path)

    assert outcome.reason == "no_progress"
    assert 5 <= outcome.turns < loop.MAX_TURNS, "必须远早于兜底上限就停下"
    assert "read_file" in outcome.detail
    assert any(
        "无进展检测" in str(message.get("content")) for message in messages
    ), "警告必须先喂给模型，它才有机会换方法"


def test_max_turns_is_reported_as_incomplete(monkeypatch, capsys):
    """验收 10：轮次用尽要明确说「未完成」，不能伪装成完成。"""
    monkeypatch.setattr(loop, "MAX_TURNS", 2)
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10**9)
    monkeypatch.setattr(loop, "provider", _StuckProvider())

    outcome = loop.agent_loop([{"role": "user", "content": "hi"}])

    assert outcome.reason == "max_turns"
    assert outcome.turns == 2
    assert not outcome.completed
    assert "未完成" in capsys.readouterr().out


def test_outcomes_for_completed_budget_and_provider_error(monkeypatch, capsys):
    """验收 11：各出口的 reason 与 turns 正确。"""
    # 完成
    monkeypatch.setattr(
        loop,
        "provider",
        type("P", (), {"chat": lambda self, **kw: _response([_text("答案")])})(),
    )
    completed = loop.agent_loop([{"role": "user", "content": "hi"}])
    assert (completed.reason, completed.turns) == ("completed", 1)
    assert completed.completed

    # token 预算
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10)
    monkeypatch.setattr(loop, "provider", _StuckProvider())
    budget = loop.agent_loop([{"role": "user", "content": "hi"}])
    assert (budget.reason, budget.turns) == ("budget", 1)
    assert budget.total_tokens > 10

    # provider 失败：turns 仍计入这次请求，但没有任何 token 消耗
    class FailingProvider:
        def chat(self, **kwargs):
            raise ProviderError("boom", retryable=False)

    monkeypatch.setattr(loop, "provider", FailingProvider())
    failed = loop.agent_loop([{"role": "user", "content": "hi"}])
    assert (failed.reason, failed.turns, failed.total_tokens) == (
        "provider_error",
        1,
        0,
    )
    assert "boom" in failed.detail


def test_outcome_for_tool_failures(monkeypatch, tmp_path):
    """连续失败仍由 failure_guard 中止，与被调大的轮次上限无关。"""
    hooks.register_hook("PostToolUse", guards.failure_guard)
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10**9)
    monkeypatch.setattr(
        loop, "provider", _StuckProvider(args={"path": "不存在.txt"})
    )

    outcome = loop.agent_loop(
        [{"role": "user", "content": "hi"}], workspace=tmp_path
    )

    assert outcome.reason == "tool_failures"
    assert outcome.turns == failure_guard.limit


def test_worker_fails_task_when_loop_does_not_complete(monkeypatch, tmp_path):
    """多 Agent：轮次用尽的 worker 不得把任务标成完成。

    这条是 LoopOutcome 的实际用途 —— 任务板上的「完成」意味着下游可以
    按已交付处理，用它来表示「跑到一半被截断了」是比崩溃更难发现的错误。
    """
    from myharness.teams.agent import TeamAgent
    from myharness.teams.messaging import MessageQueue
    from myharness.teams.taskboard import TaskBoard, TaskStatus

    board = TaskBoard(tmp_path / "board.jsonl")
    queue = MessageQueue(tmp_path / "queue")
    agent = TeamAgent("agent_a", tmp_path, board, queue)
    task_id = board.create_task("打转", "随便做点什么")

    saved_stop = list(hooks.registered_hooks("Stop"))
    hooks.HOOKS["Stop"].clear()  # Stop hook 绑着真实 provider，会发网络请求
    try:
        monkeypatch.setattr(loop, "MAX_TURNS", 2)
        monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10**9)
        monkeypatch.setattr(loop, "provider", _StuckProvider())
        result = agent.claim_and_execute()
    finally:
        hooks.HOOKS["Stop"][:] = saved_stop

    assert result["success"] is False
    assert "max_turns" in result["error"]
    assert board.get_task(task_id).status is TaskStatus.FAILED
