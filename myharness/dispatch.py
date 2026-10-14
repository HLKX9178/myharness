"""工具调度：**唯一的工具执行入口**。

所有工具调用都必须经过 execute_tool()，不得绕过它直接调用 handler。
这是 PreToolUse / PostToolUse 无法被规避的结构性保证
（见 AGENTS.md 契约 6）。

对应教程 s14 的 execute_tool(block, handlers)。
"""

from pathlib import Path
from typing import Any, Callable

from myharness.hooks import trigger_all_hooks, trigger_hooks


def execute_tool(
    block: Any,
    handlers: dict[str, Callable],
    workspace: Path | None = None,
) -> str:
    """执行单个 tool_use block，永远返回字符串。

    流程：PreToolUse -> handler -> PostToolUse

    不变量：handlers 必须与当前传给模型的工具定义来自**同一次**
    assemble_tool_pool() 快照，否则模型可能调用到没有实现的工具。

    Args:
        block: tool_use block
        handlers: 工具处理函数字典
        workspace: 工作目录（传递给 hook，用于 per-agent 策略）；None 时使用全局 WORKDIR
    """
    blocked = trigger_hooks("PreToolUse", block, workspace=workspace)
    if blocked is not None:
        return str(blocked)

    handler = handlers.get(block.name)
    if not handler:
        return f"Unknown tool: {block.name}"

    try:
        # 尝试传入 workspace 参数
        import inspect
        sig = inspect.signature(handler)
        if "workspace" in sig.parameters:
            output = str(handler(**(block.input or {}), workspace=workspace))
        else:
            output = str(handler(**(block.input or {})))
    except Exception as exc:
        # 工具报错只变成一条结果，绝不让程序崩掉（见 AGENTS.md 契约 4）
        output = f"Error: {type(exc).__name__}: {exc}"

    trigger_all_hooks("PostToolUse", block, output, workspace=workspace)
    return output
