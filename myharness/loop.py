"""Agent Loop。

反复执行「问模型 -> 执行工具 -> 回填结果」，直到模型不再调用工具，
或达到 MAX_TURNS 上限。

本模块只负责编排，不含任何具体工具实现，也不直接读写终端输入。
"""

from myharness.config import MODEL, SYSTEM, client
from myharness.tools import TOOL_HANDLERS, TOOLS
from myharness.ui import Color

MAX_TURNS = 20  # 防止模型无限循环


def agent_loop(messages: list) -> None:
    """驱动一轮完整对话，直到模型给出不再调用工具的回复。"""
    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=2000,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            return  # 模型不再调用工具 -> 这一轮结束

        results = []
        for block in tool_calls:
            print(f"{Color.YELLOW}  [tool] {block.name}({block.input}){Color.RESET}")
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = (
                    str(handler(**block.input))
                    if handler
                    else f"Unknown tool: {block.name}"
                )
            except Exception as exc:
                # 工具报错只变成一条结果，绝不让程序崩掉（见 AGENTS.md 契约 4）
                output = f"Error: {type(exc).__name__}: {exc}"
            print(f"{Color.GRAY}  [result] {output[:200]}{Color.RESET}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})

    print(f"{Color.RED}  [warn] 达到最大轮次，强制结束{Color.RESET}")
