"""内置 Hook 装配。

这是**横切能力的唯一注册点**。新增权限、审计、日志、指标、上下文注入
等能力时，在这里注册，而不是去修改 agent_loop（见 AGENTS.md 契约 6）。

对应教程 s04 末尾的这一段：

    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", log_hook)
    register_hook("PostToolUse", large_output_hook)
    register_hook("Stop", summary_hook)

注册顺序即执行顺序：

  PreToolUse   permission -> repeat_call -> log
               被拒绝的调用不应被记为"已调用过"，所以权限必须最先。

  PostToolUse  result -> failure -> large_output
               PostToolUse 不短路，三者都会收到通知。
"""

from myharness.guards import failure_guard, repeat_call_guard
from myharness.hooks import HOOKS, register_hook
from myharness.observability import (
    large_output_hook,
    log_hook,
    result_hook,
    session_summary_hook,
)
from myharness.security import permission_hook


def install_default_hooks() -> None:
    """注册全部内置 hook。

    幂等：通过检查实际注册状态判断，而不是依赖隐藏标志位 ——
    这样测试可以在 clear_hooks() 之后重新装配。
    """
    if permission_hook in HOOKS["PreToolUse"]:
        return

    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", repeat_call_guard)
    register_hook("PreToolUse", log_hook)

    register_hook("PostToolUse", result_hook)
    register_hook("PostToolUse", failure_guard)
    register_hook("PostToolUse", large_output_hook)

    # UserPromptSubmit 暂无内置 hook；M3 的记忆检索将注册在这里
    register_hook("Stop", session_summary_hook)
