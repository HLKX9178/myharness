"""Agent Loop。

反复执行「问模型 -> 执行工具 -> 回填结果」，直到模型不再调用工具，
或达到 MAX_TURNS 上限。

本模块只负责编排，不含任何具体工具实现，也不直接读写终端输入。
"""

from myharness.config import MODEL, SYSTEM, client
from myharness.tools import TOOL_HANDLERS, TOOLS
from myharness.ui import Color

MAX_TURNS = 20  # 防止模型无限循环
MAX_TOTAL_TOKENS = 100000  # token 上限
MAX_CONSECUTIVE_FAILURES = 3  # 连续失败上限

def agent_loop(messages: list) -> None:
    """驱动一轮完整对话，直到模型给出不再调用工具的回复。"""
    total_tokens = 0
    consecutive_failures = 0
    last_tool_call = None  # 格式: (name, input_hash)

    for turn in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=2000,
        )
        messages.append({"role": "assistant", "content": response.content})

        # 累计 token 使用量
        total_tokens += response.usage.input_tokens + response.usage.output_tokens
        print(f"{Color.GRAY}  [tokens] turn {turn+1}: +{response.usage.input_tokens + response.usage.output_tokens} (total: {total_tokens}){Color.RESET}")

        if total_tokens > MAX_TOTAL_TOKENS:
            print(f"{Color.RED}  [warn] 达到 token 上限 ({MAX_TOTAL_TOKENS})，强制结束{Color.RESET}")
            break

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            return  # 模型不再调用工具 -> 这一轮结束

        # 检测重复调用（只检查第一个工具调用）
        if tool_calls:
            first_call = tool_calls[0]
            current_call = (first_call.name, str(sorted(first_call.input.items())))
            if current_call == last_tool_call:
                print(f"{Color.YELLOW}  [warn] 检测到重复调用，给模型反馈{Color.RESET}")
                messages.append({
                    "role": "user",
                    "content": "你刚才已经用相同参数调用过这个工具了。如果还需要信息，请换一个方法或调整参数。"
                })
                last_tool_call = None  # 重置，避免连续两次触发
                continue  # 跳过本轮工具执行，直接让模型重新思考
            last_tool_call = current_call

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

        # 检测连续失败
        if all(r["content"].startswith("Error:") for r in results):
            consecutive_failures += 1
            print(f"{Color.YELLOW}  [warn] 连续失败 {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}{Color.RESET}")

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"{Color.RED}  [error] 连续 {MAX_CONSECUTIVE_FAILURES} 轮工具全部失败，终止{Color.RESET}")
                break
            elif consecutive_failures >= 2:
                # 第2次失败时给模型反馈，还有1次机会
                messages.append({
                    "role": "user",
                    "content": "连续工具调用失败。请检查参数格式、路径是否正确，或尝试换一种方法。"
                })
        else:
            consecutive_failures = 0  # 有成功就重置

    if turn == MAX_TURNS - 1:
        print(f"{Color.RED}  [warn] 达到最大轮次 ({MAX_TURNS})，强制结束{Color.RESET}")
