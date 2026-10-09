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

  UserPromptSubmit  (Phase 5: 不再注入，记忆通过 system prompt 加载)
"""

from myharness.guards import failure_guard, repeat_call_guard
from myharness.hooks import HOOKS, register_hook
from myharness.memory import MemoryStore
from myharness.observability import (
    large_output_hook,
    log_hook,
    result_hook,
    session_summary_hook,
)
from myharness.security import permission_hook


def install_default_hooks(memory_store: MemoryStore | None = None, provider=None) -> None:
    """注册全部内置 hook。

    幂等：通过检查实际注册状态判断，而不是依赖隐藏标志位 ——
    这样测试可以在 clear_hooks() 之后重新装配。

    memory_store 显式传入而不是在此导入全局实例，测试可传入临时存储。
    provider 用于 extract_memories_hook，需要调用模型提取记忆。
    """
    # Phase 5：recall_hook 不再需要，记忆通过 system prompt 注入
    # if memory_store is not None and memory_store.recall_hook not in HOOKS["UserPromptSubmit"]:
    #     register_hook("UserPromptSubmit", memory_store.recall_hook)

    if permission_hook in HOOKS["PreToolUse"]:
        return

    register_hook("PreToolUse", permission_hook)
    register_hook("PreToolUse", repeat_call_guard)
    register_hook("PreToolUse", log_hook)

    register_hook("PostToolUse", result_hook)
    register_hook("PostToolUse", failure_guard)
    register_hook("PostToolUse", large_output_hook)

    register_hook("Stop", session_summary_hook)

    # Phase 3：自动提取记忆
    if memory_store is not None and provider is not None:
        # 用 lambda 绑定 provider，避免在 hook 里导入 config
        extract_hook = lambda messages: memory_store.extract_memories_hook(messages, provider)
        if extract_hook not in HOOKS["Stop"]:
            register_hook("Stop", extract_hook)
