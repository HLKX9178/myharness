"""工具调用守卫：以 hook 形式实现的调用策略。

对应 AGENTS.md 契约 6 —— 策略挂在 hook 上，机制留在 loop。

两个守卫：

  RepeatCallGuard          PreToolUse   拦截参数完全相同的连续重复调用
  ConsecutiveFailureGuard  PostToolUse  统计连续失败，决定告警与中止

关于状态：这两个守卫是**有状态的 hook**。跨轮状态由 agent_loop 在
每次对话轮次开始时通过 reset_turn_state() 清空 —— 否则用户第二遍问
同样的问题时，RepeatCallGuard 会误拦。
"""

from typing import Any


class RepeatCallGuard:
    """PreToolUse：拦截参数完全相同的连续重复调用。

    完全迁移自 agent_loop，并顺带修掉一个协议问题：

        原实现是「跳过本轮工具执行 + 追加一条 user 文本消息」，
        这会让 assistant 的 tool_use 缺少配对的 tool_result。

    现在改为由 hook 返回拦截消息 —— 它本身就是该 tool_use 对应的
    tool_result，消息协议始终合法。
    """

    def __init__(self) -> None:
        self._last: tuple[str, str] | None = None

    def __call__(self, block: Any) -> str | None:
        current = (block.name, str(sorted((block.input or {}).items())))

        if current == self._last:
            self._last = None  # 只拦一次，避免永久锁死
            return (
                "你刚才已经用相同参数调用过这个工具了。"
                "如果还需要信息，请换一个方法或调整参数。"
            )

        self._last = current
        return None

    def reset(self) -> None:
        self._last = None


class ConsecutiveFailureGuard:
    """PostToolUse：统计连续失败的工具调用。

    只负责**策略** —— 什么算失败、阈值多少、何时告警。
    「终止循环」是 loop 的机制，由 loop 读取 should_abort 后执行。

    语义说明：原实现按「一轮内全部工具失败」计数，现在按「连续失败的
    单个工具调用」计数。也就是说，如果一轮里有 3 个工具全部失败，
    原实现记 1 次，现在记 3 次 —— 触发更早。阈值可通过构造参数调整。
    """

    def __init__(self, limit: int = 3, warn_after: int = 2) -> None:
        self.limit = limit
        self.warn_after = warn_after
        self.failures = 0
        self._feedback: str | None = None

    def __call__(self, block: Any, output: str) -> None:
        if str(output).startswith("Error:"):
            self.failures += 1
            if self.failures == self.warn_after:
                self._feedback = (
                    "连续工具调用失败。请检查参数格式、路径是否正确，"
                    "或尝试换一种方法。"
                )
        else:
            self.failures = 0
            self._feedback = None
        return None

    @property
    def should_abort(self) -> bool:
        return self.failures >= self.limit

    def take_feedback(self) -> str | None:
        """取出待注入的反馈消息（取一次即清空）。"""
        feedback, self._feedback = self._feedback, None
        return feedback

    def reset(self) -> None:
        self.failures = 0
        self._feedback = None


# hooks 需要可调用对象，loop 需要查询状态，因此在这里持有实例。
# 这是 AGENTS.md「禁止可变全局状态」的**受控例外**：状态是本轮对话内的，
# 且必须由 agent_loop 在每轮开始时清空。
repeat_call_guard = RepeatCallGuard()
failure_guard = ConsecutiveFailureGuard()


def reset_turn_state() -> None:
    """清空守卫的跨轮状态。由 agent_loop 在每轮对话开始时调用。"""
    repeat_call_guard.reset()
    failure_guard.reset()
