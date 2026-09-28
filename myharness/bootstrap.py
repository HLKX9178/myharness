"""内置 Hook 装配。

这是**横切能力的唯一注册点**。新增权限、审计、日志、指标、上下文注入
等能力时，在这里注册，而不是去修改 agent_loop（见 AGENTS.md 契约 6）。

对应教程 s04 末尾的这一段：

    register_hook("UserPromptSubmit", context_inject_hook)
    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", log_hook)
    register_hook("PostToolUse", large_output_hook)
    register_hook("Stop", summary_hook)

注册顺序即执行顺序。PreToolUse 中 permission_hook 必须排在 log_hook
之前：被拒绝的调用不应产生"已调用"的日志。
"""

from myharness.hooks import register_hook
from myharness.observability import (
    context_inject_hook,
    large_output_hook,
    log_hook,
    result_hook,
    session_summary_hook,
)
from myharness.security import permission_hook

_installed = False


def install_default_hooks() -> None:
    """注册全部内置 hook。重复调用无副作用。"""
    global _installed
    if _installed:
        return

    # 权限判定必须最先执行：拒绝即短路，后续 hook 不再运行
    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", log_hook)

    register_hook("PostToolUse", result_hook)
    register_hook("PostToolUse", large_output_hook)

    register_hook("UserPromptSubmit", context_inject_hook)
    register_hook("Stop", session_summary_hook)

    _installed = True
