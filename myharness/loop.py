"""Agent Loop。

反复执行「问模型 -> 执行工具 -> 回填结果」，直到模型不再调用工具。

设计约束（见 AGENTS.md 契约 6）：**本模块不含任何横切逻辑**。

  - 权限、审计、日志、结果展示  -> hook（bootstrap.py 注册）
  - 重复调用拦截               -> hook（guards.RepeatCallGuard）
  - 连续失败统计与告警策略       -> hook（guards.ConsecutiveFailureGuard）
  - 工具执行                   -> dispatch.execute_tool() 唯一入口

留在这里的是**循环自身的机制**，不是策略：

  - MAX_TURNS / token 预算      跨轮累计的循环不变量
  - 上下文压缩                  需要替换整个 messages 列表
  - 读取守卫状态并执行终止/注入    loop 层面的动作，策略由 guards.py 决定
  - 清空守卫跨轮状态             对话轮次的边界在本函数上
  - 模型调用失败时收尾            重试与可重试性判定在 providers 层，
                                这里只负责「本轮到此为止」并说清原因
"""

from pathlib import Path
from typing import Any

from myharness.config import MAX_TOKENS, build_system_prompt, get_workdir, provider
from myharness.context import ContextManager
from myharness.dispatch import execute_tool
from myharness.guards import failure_guard, reset_turn_state
from myharness.hooks import trigger_hooks
from myharness.providers.base import ProviderError
from myharness.tools import assemble_tool_pool
from myharness.ui import Color

MAX_TURNS = 20  # 防止模型无限循环
MAX_TOTAL_TOKENS = 100000  # 本轮累计预算；单次请求上限见 config.MAX_TOKENS

# 上下文管理器
context_manager = ContextManager(max_messages=30)


def _block_to_dict(block: Any) -> dict:
    """把 provider 返回的 content block 归一成可序列化的 dict。

    Anthropic SDK 的 block 是 pydantic 对象（自带 model_dump），OpenAI 适配层
    返回的轻量对象也补了 model_dump。但 provider 契约只要求「有 type」——
    无条件调用 model_dump() 等于把「必须长得像 pydantic」变成一条没写下来的
    隐式约定，任何不满足它的 provider 都会让整轮对话崩掉。这里对 dict /
    数据类 / 命名空间形态做兜底。
    """
    if isinstance(block, dict):
        return block

    dump = getattr(block, "model_dump", None)
    if callable(dump):
        return dump()

    data = dict(vars(block)) if hasattr(block, "__dict__") else {}
    if "type" not in data and hasattr(block, "type"):
        # type 可能是类属性，不会出现在 vars() 里
        data["type"] = block.type
    return data


def _report_provider_error(error: ProviderError) -> None:
    """把模型调用失败的原因与修复建议讲清楚。

    这是**诊断输出**，不是横切策略：什么算可重试、重试几次，都在 providers
    层决定（见 providers/base.py）。这里只负责把已经定论的结果说给人听，
    并保证 messages 不被写入半截状态 —— 失败时最后一条消息仍是用户输入，
    下一轮可以直接重问同一个问题。
    """
    attempts = f"（已尝试 {error.attempts} 次）" if error.attempts > 1 else ""
    print(f"{Color.RED}  [error] 模型调用失败{attempts}: {error.message}{Color.RESET}")
    if error.hint:
        print(f"{Color.RED}  [error] {error.hint}{Color.RESET}")
    print(f"{Color.GRAY}  本轮已结束，历史保留，修正后可重新提问。{Color.RESET}")


def agent_loop(messages: list, workspace: Path | None = None) -> None:
    """驱动一轮完整对话，直到模型给出不再调用工具的回复。

    Args:
        messages: 消息历史
        workspace: 工作目录；None 时使用全局 config.WORKDIR
    """
    # 对话轮次边界：清空有状态 hook 的跨轮数据
    reset_turn_state()

    # 未传入时使用全局 WORKDIR
    if workspace is None:
        workspace = get_workdir()

    total_tokens = 0

    # Phase 5：提取用户查询，用于构建动态 system prompt
    user_query = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                user_query = content
                break

    # 构建包含相关记忆的 system prompt
    system_prompt = build_system_prompt(user_query)

    for turn in range(MAX_TURNS):
        # 压缩上下文（如果需要）
        if context_manager.should_compress(messages):
            print(f"{Color.YELLOW}  [context] 压缩历史消息: {len(messages)} -> ", end="")
            messages[:] = context_manager.compress(messages)
            print(f"{len(messages)}{Color.RESET}")

        # 每轮重新组装工具池：M4 接入 MCP 后，新工具下一轮即可见
        tools, handlers = assemble_tool_pool(workspace)

        try:
            response = provider.chat(
                messages=messages,
                tools=tools,
                system=system_prompt,
                max_tokens=MAX_TOKENS,
            )
        except ProviderError as error:
            # providers 层已把 SDK 异常规范化（契约 4）。本轮无法继续，但必须
            # **优雅结束**：把异常抛穿会让 CLI 崩掉，连尚未保存的会话一起丢掉。
            # 只捕获 ProviderError —— 其它异常是代码缺陷，应当暴露出来。
            _report_provider_error(error)
            return

        # 将 ContentBlock 转成字典（支持序列化）
        content_dict = [_block_to_dict(block) for block in response.content]
        messages.append({"role": "assistant", "content": content_dict})

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

        # 唯一的工具执行入口：PreToolUse -> handler -> PostToolUse
        # 重复调用拦截与权限判定都在 PreToolUse 上，此处不感知
        results = []
        for block in tool_calls:
            output = execute_tool(block, handlers, workspace)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })

        messages.append({"role": "user", "content": results})

        # 连续失败的**策略**在 guards.py；这里只执行 loop 层面的动作
        if failure_guard.should_abort:
            print(
                f"{Color.RED}  [error] 连续 {failure_guard.limit} 次工具调用失败，"
                f"终止{Color.RESET}"
            )
            return

        feedback = failure_guard.take_feedback()
        if feedback:
            messages.append({"role": "user", "content": feedback})

    print(f"{Color.RED}  [warn] 达到最大轮次 ({MAX_TURNS})，强制结束{Color.RESET}")
