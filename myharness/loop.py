"""Agent Loop。

反复执行「问模型 -> 执行工具 -> 回填结果」，直到模型不再调用工具。

设计约束（见 AGENTS.md 契约 6）：**本模块不含任何横切逻辑**。
权限、审计、日志、结果展示全部通过 hook 注册（bootstrap.py）。
工具执行只经过 dispatch.execute_tool() 这一个入口。

留在这里的是**循环自身的机制**，不是策略：
  - MAX_TURNS / token 预算 / 连续失败计数：需要跨轮累计状态
  - 上下文压缩：需要替换整个 messages 列表
  - 重复调用检测：需要记住上一轮的调用
"""

from myharness.config import SYSTEM, provider
from myharness.context import ContextManager
from myharness.dispatch import execute_tool
from myharness.hooks import trigger_hooks
from myharness.tools import assemble_tool_pool
from myharness.ui import Color

MAX_TURNS = 20  # 防止模型无限循环
MAX_TOTAL_TOKENS = 100000  # token 上限
MAX_CONSECUTIVE_FAILURES = 3  # 连续失败上限

# 上下文管理器
context_manager = ContextManager(max_messages=30)


def agent_loop(messages: list) -> None:
    """驱动一轮完整对话，直到模型给出不再调用工具的回复。"""
    total_tokens = 0
    consecutive_failures = 0
    last_tool_call = None  # 格式: (name, input)

    for turn in range(MAX_TURNS):
        # 压缩上下文（如果需要）
        if context_manager.should_compress(messages):
            print(f"{Color.YELLOW}  [context] 压缩历史消息: {len(messages)} -> ", end="")
            messages[:] = context_manager.compress(messages)
            print(f"{len(messages)}{Color.RESET}")

        # 每轮重新组装工具池：M4 接入 MCP 后，新工具下一轮即可见
        tools, handlers = assemble_tool_pool()

        response = provider.chat(
            messages=messages,
            tools=tools,
            system=SYSTEM,
            max_tokens=2000,
        )
        messages.append({"role": "assistant", "content": response.content})

        used = response.usage.input_tokens + response.usage.output_tokens
        total_tokens += used
        print(f"{Color.GRAY}  [tokens] turn {turn + 1}: +{used} (total: {total_tokens}){Color.RESET}")

        if total_tokens > MAX_TOTAL_TOKENS:
            print(f"{Color.RED}  [warn] 达到 token 上限 ({MAX_TOTAL_TOKENS})，强制结束{Color.RESET}")
            return

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            # Stop hook：返回非 None 可强制续跑（对应教程 s04 的 Stop 语义）
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return

        # 检测重复调用（只检查第一个工具调用）
        first_call = tool_calls[0]
        current_call = (first_call.name, str(sorted((first_call.input or {}).items())))
        if current_call == last_tool_call:
            print(f"{Color.YELLOW}  [warn] 检测到重复调用，给模型反馈{Color.RESET}")
            messages.append({
                "role": "user",
                "content": "你刚才已经用相同参数调用过这个工具了。如果还需要信息，请换一个方法或调整参数。",
            })
            last_tool_call = None  # 重置，避免连续两次触发
            continue  # 跳过本轮工具执行，直接让模型重新思考
        last_tool_call = current_call

        # 唯一的工具执行入口：PreToolUse -> handler -> PostToolUse
        results = []
        for block in tool_calls:
            output = execute_tool(block, handlers)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": results})

        # 检测连续失败
        if all(str(r["content"]).startswith("Error:") for r in results):
            consecutive_failures += 1
            print(f"{Color.YELLOW}  [warn] 连续失败 {consecutive_failures}/{MAX_CONSECUTIVE_FAILURES}{Color.RESET}")

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"{Color.RED}  [error] 连续 {MAX_CONSECUTIVE_FAILURES} 轮工具全部失败，终止{Color.RESET}")
                return
            if consecutive_failures >= 2:
                # 第 2 次失败时给模型反馈，还有 1 次机会
                messages.append({
                    "role": "user",
                    "content": "连续工具调用失败。请检查参数格式、路径是否正确，或尝试换一种方法。",
                })
        else:
            consecutive_failures = 0  # 有成功就重置

    print(f"{Color.RED}  [warn] 达到最大轮次 ({MAX_TURNS})，强制结束{Color.RESET}")
