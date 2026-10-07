"""Hook 机制：扩展逻辑挂在 hook 上，Agent Loop 保持干净。

这是教程 s04 的核心设计（见 AGENTS.md 契约 6）：

    s03: if not check_permission(block): ...
    s04: if trigger_hooks("PreToolUse", block): ...

四个扩展点：

    UserPromptSubmit  用户输入进入模型之前
    PreToolUse        工具执行之前（返回非 None 即拦截）
    PostToolUse       工具执行之后（只观察，不改变结果）
    Stop              本轮循环即将结束（返回非 None 可强制续跑）

语义约定（PreToolUse / Stop）：

  - hook 返回 None  -> 放行
  - hook 返回其它值  -> 拦截，且该值短路后续 hook，并成为 tool_result

UserPromptSubmit 语义不同，由 submit_user_prompt() 触发：

  - None      -> 不处理
  - str       -> 注入上下文；多个 hook 的结果按注册顺序合并，不短路
  - Block(..) -> 拒绝本次输入，短路后续 hook，输入不进入 messages

这个模块**不依赖项目内任何其它模块**，以便被任意层安全导入。
"""

from dataclasses import dataclass
from typing import Any, Callable

HookCallback = Callable[..., Any]


@dataclass(frozen=True)
class Block:
    """UserPromptSubmit hook 的拒绝结果。reason 展示给用户，不发给模型。"""

    reason: str

HOOKS: dict[str, list[HookCallback]] = {
    "UserPromptSubmit": [],
    "PreToolUse": [],
    "PostToolUse": [],
    "Stop": [],
}


def register_hook(event: str, callback: HookCallback) -> None:
    """在某个事件上注册一个 hook。

    同一事件可以注册多个，按注册顺序执行。
    """
    if event not in HOOKS:
        raise ValueError(f"Unknown hook event: {event!r}")
    HOOKS[event].append(callback)


def trigger_hooks(event: str, *args: Any) -> Any:
    """触发某个事件的全部 hook。

    返回第一个非 None 的结果并立即短路；全部返回 None 时返回 None。
    """
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:
            return result
    return None


def trigger_all_hooks(event: str, *args: Any) -> None:
    """触发全部 hook 并忽略返回值。

    用于 PostToolUse 这类「只观察、不干预」的事件：任何一个 hook 的
    返回值都不应该阻止其它 hook 收到通知，否则它们会静默丢失事件。
    """
    for callback in HOOKS[event]:
        callback(*args)


def submit_user_prompt(query: str) -> str | Block:
    """触发 UserPromptSubmit，返回最终进入 messages 的用户消息，或 Block。

    注入内容放在用户原文之前，用 <context> 包裹，模型能区分哪部分是用户说的。
    返回值始终是纯字符串消息：context.py 依赖"字符串 content = 新轮次起点"
    来判断压缩截断点，这里不能改成 block 列表。
    """
    injected: list[str] = []
    for callback in HOOKS["UserPromptSubmit"]:
        result = callback(query)
        if isinstance(result, Block):
            return result
        if isinstance(result, str) and result.strip():
            injected.append(result.strip())
        elif result is not None and not isinstance(result, str):
            raise TypeError(
                f"UserPromptSubmit hook {getattr(callback, '__name__', callback)!r} "
                f"返回了 {type(result).__name__}，只允许 None / str / Block"
            )

    if not injected:
        return query
    context = "\n\n".join(injected)
    return f"<context>\n{context}\n</context>\n\n{query}"


def registered_hooks(event: str) -> list[HookCallback]:
    """返回某事件已注册的 hook 列表（只读快照，供测试断言）。"""
    return list(HOOKS[event])


def clear_hooks() -> None:
    """清空全部 hook。仅供测试使用。"""
    for event in HOOKS:
        HOOKS[event].clear()
