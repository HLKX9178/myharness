"""可观测性 Hook：工具日志、大输出告警、会话小结。

这些都是**横切关注点**。按 AGENTS.md 契约 6，它们挂在 hook 上，
而不是写进 agent_loop —— 这样 Loop 才能保持干净。

本模块不导入 config，以免形成 config -> bootstrap -> observability -> config
的循环依赖。
"""

from typing import Any

from myharness.ui import Color

# 超过这个长度就告警（字符数）
LARGE_OUTPUT_THRESHOLD = 100_000


def log_hook(block: Any) -> None:
    """PreToolUse：记录每一次工具调用。"""
    values = list((block.input or {}).values())[:2]
    preview = str(values)[:60]
    print(f"{Color.YELLOW}  [tool] {block.name}({preview}){Color.RESET}")
    return None


def result_hook(block: Any, output: str) -> None:
    """PostToolUse：展示工具结果的前 200 个字符。"""
    print(f"{Color.GRAY}  [result] {str(output)[:200]}{Color.RESET}")
    return None


def large_output_hook(block: Any, output: str) -> None:
    """PostToolUse：输出过大时告警。"""
    if len(str(output)) > LARGE_OUTPUT_THRESHOLD:
        print(
            f"{Color.YELLOW}  [hook] Large output from {block.name}: "
            f"{len(str(output))} chars{Color.RESET}"
        )
    return None


def session_summary_hook(messages: list) -> None:
    """Stop：本轮循环即将结束时输出小结。"""
    tool_count = sum(
        1
        for message in messages
        for block in (
            message.get("content")
            if isinstance(message.get("content"), list)
            else []
        )
        if isinstance(block, dict) and block.get("type") == "tool_result"
    )
    print(f"{Color.GRAY}  [hook] Stop: session used {tool_count} tool calls{Color.RESET}")
    return None
