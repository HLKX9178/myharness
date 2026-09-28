"""Hook 机制：扩展逻辑挂在 hook 上，Agent Loop 保持干净。

这是教程 s04 的核心设计（见 AGENTS.md 契约 6）：

    s03: if not check_permission(block): ...
    s04: if trigger_hooks("PreToolUse", block): ...

四个扩展点：

    UserPromptSubmit  用户输入进入模型之前
    PreToolUse        工具执行之前（返回非 None 即拦截）
    PostToolUse       工具执行之后（只观察，不改变结果）
    Stop              本轮循环即将结束（返回非 None 可强制续跑）

语义约定：

  - hook 返回 None  -> 放行
  - hook 返回其它值  -> 拦截，且该值短路后续 hook，并成为 tool_result

这个模块**不依赖项目内任何其它模块**，以便被任意层安全导入。
"""

from typing import Any, Callable

HookCallback = Callable[..., Any]

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


def registered_hooks(event: str) -> list[HookCallback]:
    """返回某事件已注册的 hook 列表（只读快照，供测试断言）。"""
    return list(HOOKS[event])


def clear_hooks() -> None:
    """清空全部 hook。仅供测试使用。"""
    for event in HOOKS:
        HOOKS[event].clear()
