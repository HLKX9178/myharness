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

  PostToolUse  result -> failure -> progress -> large_output
               PostToolUse 不短路，四者都会收到通知。

               failure 与 progress 的分工：前者「连续 Error」计数（遇成功清零），
               后者「同指纹同结果」计数（不管成功失败）。一成一败的循环只有
               后者抓得住，所以两者必须独立，不要合并成一个计数器。

  UserPromptSubmit  recall -> 注入相关记忆（拼进用户消息，不碰 system）
"""

from myharness.guards import failure_guard, progress_guard, repeat_call_guard
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
    # 记忆注入用户消息，**不进 system prompt**。
    #
    # system prompt 位于请求前缀（[tools][system][messages...]）的中间，往里放
    # 任何随提问变化的内容（哪怕只是三条记忆的排序不同），后面整段历史都会
    # 失去前缀缓存。用户消息本来就是每轮新增的，注入在那里不额外制造失效。
    #
    # submit_user_prompt 会把它包成 <context>...</context> 放在用户原文之前，
    # 消息仍是纯字符串（context.py 依赖这一点判断轮次边界）。
    if memory_store is not None and memory_store.recall_hook not in HOOKS["UserPromptSubmit"]:
        register_hook("UserPromptSubmit", memory_store.recall_hook)

    if permission_hook not in HOOKS["PreToolUse"]:
        register_hook("PreToolUse", permission_hook)
        register_hook("PreToolUse", repeat_call_guard)
        register_hook("PreToolUse", log_hook)

        register_hook("PostToolUse", result_hook)
        register_hook("PostToolUse", failure_guard)
        # progress 必须排在 failure 之后：两者都会看每一条结果，
        # 顺序不影响判定，但排在一起便于阅读「先失败、再进展」
        register_hook("PostToolUse", progress_guard)
        register_hook("PostToolUse", large_output_hook)

        register_hook("Stop", session_summary_hook)

    # 独立于上面的检查：记忆提取必须排在 session_summary_hook 之后
    if memory_store is not None and provider is not None:
        extract_hook = memory_store.extraction_hook(provider)
        if extract_hook not in HOOKS["Stop"]:
            register_hook("Stop", extract_hook)
