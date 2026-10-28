"""Agent Loop。

反复执行「问模型 -> 执行工具 -> 回填结果」，直到模型不再调用工具。

设计约束（见 AGENTS.md 契约 6）：**本模块不含任何横切逻辑**。

  - 权限、审计、日志、结果展示  -> hook（bootstrap.py 注册）
  - 重复调用拦截               -> hook（guards.RepeatCallGuard）
  - 连续失败统计与告警策略       -> hook（guards.ConsecutiveFailureGuard）
  - 无进展检测                 -> hook（guards.ProgressGuard）
  - 工具执行                   -> dispatch.execute_tool() 唯一入口

留在这里的是**循环自身的机制**，不是策略：

  - MAX_TURNS / token 预算      跨轮累计的循环不变量
  - 上下文压缩                  需要替换整个 messages 列表
  - 读取守卫状态并执行终止/注入    loop 层面的动作，策略由 guards.py 决定
  - 清空守卫跨轮状态             对话轮次的边界在本函数上
  - 模型调用失败时收尾            重试与可重试性判定在 providers 层，
                                这里只负责「本轮到此为止」并说清原因
  - 返回 LoopOutcome             终止原因必须可辨：被截断不等于完成
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

from myharness.config import (
    MAX_CONTEXT_TOKENS,
    MAX_TOKENS,
    MAX_TURNS,
    TURN_TOKEN_BUDGET,
    build_system_prompt,
    get_workdir,
    provider,
)
from myharness.context import ContextManager, estimate_messages_tokens
from myharness.dispatch import execute_tool
from myharness.guards import failure_guard, progress_guard, reset_turn_state
from myharness.hooks import trigger_hooks
from myharness.providers.base import ProviderError, cached_prompt_tokens
from myharness.tools import assemble_tool_pool
from myharness.ui import Color

# 终止原因。前三个是「正常或可控地停下」，后三个是「没干完」——
# 调用方（CLI / 调度器 / 多 Agent worker）据此判断要不要重试或上报失败。
LoopReason = Literal[
    "completed",
    "max_turns",
    "budget",
    "tool_failures",
    "no_progress",
    "provider_error",
]


@dataclass(frozen=True)
class LoopOutcome:
    """一轮对话的结束方式。

    `turns` 是**已发起的模型请求次数**（不是工具调用次数），`total_tokens`
    是这一轮累计消耗（每轮重发历史，所以它比上下文大得多）。

    返回值最初是 None，现在改成数据类：`agent_loop` 的所有调用方都忽略
    返回值，所以这项改动对它们是透明的；但需要区分「完成」与「被截断」
    的地方（调度器把结果记成 ok/error、多 Agent worker 决定是否交任务）
    从此有了判据 —— 让没干完的轮次看起来像干完了，是比崩溃更难发现的错误。
    """

    reason: LoopReason
    turns: int
    total_tokens: int
    detail: str = ""

    @property
    def completed(self) -> bool:
        return self.reason == "completed"


# 上下文按 token 估算压缩，而不是按固定条数：一条大 tool_result 就能顶满预算。
# max_messages 只是很松的兜底（防止极短消息把列表撑长），真正生效的是
# max_tokens（见 config.MAX_CONTEXT_TOKENS）。取 200 而不是几十：条数少不代表
# 内容少，反之亦然 —— 按条数过早丢历史，正是「聊两轮就忘」的来源。
context_manager = ContextManager(
    max_messages=200,
    max_tokens=MAX_CONTEXT_TOKENS,
)


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


def _notify_stream(on_text, event: str) -> None:
    """把「一条 assistant 消息开始 / 结束」告诉流式回调（可选协议）。

    回调可以是普通函数（测试、脚本），也可以带 `begin_message` /
    `end_message` 方法（`ui.StreamPrinter`）—— 后者需要边界来打前缀、收尾换行。
    用可选方法而不是扩充回调签名：不关心边界的调用方不该被迫实现空方法。
    """
    if on_text is None:
        return
    handler = getattr(on_text, event, None)
    if callable(handler):
        handler()


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


def _answer_pending_tool_uses(
    messages: list,
    content: list[dict],
    reason: str,
) -> None:
    """给**未执行**的 tool_use 补上配对的 tool_result。

    Anthropic 兼容端点要求 assistant 的每个 tool_use 后面必须跟一条同
    ``tool_use_id`` 的 tool_result（契约 2：消息用模型 API 原生格式）。
    循环在任何位置提前退出，都必须先把已经写进历史的 tool_use 补齐：

      - 不补 → 历史不合法，下一轮请求可能 400；即使端点宽容，模型看到的
        也是一段「问了一半」的对话，会反复重试同一个调用。
      - 该退出点恰恰是 token 预算耗尽，再执行工具只会让上下文更大，
        所以这里补的是**未执行**的说明，而不是伪造一条成功结果
        （契约 4：错误是数据，不是异常）。
    """
    pending = [block for block in content if block.get("type") == "tool_use"]
    if not pending:
        return

    results = []
    for block in pending:
        tool_use_id = block.get("id")
        if not tool_use_id:
            # 没有 id 就配不上，补了也是非法消息；至少让原因可见
            print(
                f"{Color.RED}  [warn] tool_use 缺少 id，无法补配 tool_result{Color.RESET}"
            )
            continue
        results.append({
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": reason,
            "is_error": True,
        })

    if results:
        messages.append({"role": "user", "content": results})


def _repair_orphan_tool_uses(messages: list) -> int:
    """补齐历史中悬空的 tool_use，返回修复条数。

    为什么入口还要再查一遍：``_answer_pending_tool_uses`` 只保证**本次**
    退出时历史合法，但已经落盘的会话文件可能带着旧版本留下的悬空调用
    （token 超限直接 return 的那个 bug）。一旦这种历史被重新加载，之后
    每一轮请求都会 400 —— 用户看到的是「修了还是报错」。

    这里只做协议修复，不臆造执行结果：补进去的内容明确写着未执行。
    """
    repaired = 0
    index = 0

    while index < len(messages):
        message = messages[index]
        if message.get("role") != "assistant":
            index += 1
            continue

        content = message.get("content")
        if not isinstance(content, list):
            index += 1
            continue

        use_ids = [
            block.get("id")
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]
        if not use_ids:
            index += 1
            continue

        following = messages[index + 1] if index + 1 < len(messages) else None
        is_result_message = (
            following is not None
            and following.get("role") == "user"
            and isinstance(following.get("content"), list)
            and any(
                isinstance(block, dict) and block.get("type") == "tool_result"
                for block in following["content"]
            )
        )

        answered = set()
        if is_result_message:
            answered = {
                block.get("tool_use_id")
                for block in following["content"]
                if isinstance(block, dict) and block.get("type") == "tool_result"
            }

        missing = [use_id for use_id in use_ids if use_id and use_id not in answered]
        if not missing:
            index += 2  # 配对完整，连同结果消息一起跳过
            continue

        filler = [
            {
                "type": "tool_result",
                "tool_use_id": use_id,
                "content": "Error: 该工具调用未执行（历史中断，结果缺失）。",
                "is_error": True,
            }
            for use_id in missing
        ]

        if is_result_message:
            following["content"].extend(filler)
        else:
            messages.insert(index + 1, {"role": "user", "content": filler})

        repaired += len(missing)
        index += 2

    return repaired


def agent_loop(
    messages: list,
    workspace: Path | None = None,
    on_text: Callable[[str], None] | None = None,
) -> LoopOutcome:
    """驱动一轮完整对话，直到模型给出不再调用工具的回复。

    Args:
        messages: 消息历史
        workspace: 工作目录；None 时使用全局 config.WORKDIR
        on_text: 模型文本分片回调（流式展示）；None 表示一次性拿到完整回复。
            只是**展示方式**的差异：无论传不传，写进 messages 的都是完整的
            assistant 消息。

    Returns:
        LoopOutcome：以什么原因结束、用了几次请求、花了多少 token。
        六种出口都会被显式区分 —— 「被截断」不允许被当成「完成」。
    """
    # 对话轮次边界：清空有状态 hook 的跨轮数据
    reset_turn_state()

    # 历史可能来自旧版本落盘的脏会话（见 _repair_orphan_tool_uses）。
    # 必须在第一次请求之前修，否则这一轮从第一步就 400。
    repaired = _repair_orphan_tool_uses(messages)
    if repaired:
        print(
            f"{Color.YELLOW}  [warn] 历史中有 {repaired} 个 tool_use 缺少结果，"
            f"已补齐为「未执行」{Color.RESET}"
        )

    # 未传入时使用全局 WORKDIR
    if workspace is None:
        workspace = get_workdir()

    total_tokens = 0
    turns = 0  # 已发起的模型请求次数（含失败的那次）

    # system prompt 不接收用户输入：它必须跨请求逐字节相同，否则前缀缓存
    # 从 system 就断了，后面整段历史一起重算。记忆的注入点在用户消息里
    # （UserPromptSubmit hook），那里本来就是每轮新增的内容。
    #
    # workspace 必须显式传入：它可能不是全局 WORKDIR（TeamAgent 的每个
    # worker 有自己的子目录），否则 system prompt 会告诉模型一个错误的位置
    system_prompt = build_system_prompt(workspace=workspace)

    for _ in range(MAX_TURNS):
        turns += 1
        # 压缩上下文（如果需要）。按 token 估算而不是条数：一条大 tool_result
        # 就能把预算吃满，按条数看却只有「1 条消息」。
        if context_manager.should_compress(messages):
            before_count, before_tokens = len(messages), context_manager.estimate(messages)
            messages[:] = context_manager.compress(messages)
            after_tokens = context_manager.estimate(messages)
            print(
                f"{Color.YELLOW}  [context] 压缩历史: {before_count} -> {len(messages)} 条, "
                f"~{before_tokens} -> ~{after_tokens} tokens{Color.RESET}"
            )

        # 每轮重新组装工具池：M4 接入 MCP 后，新工具下一轮即可见
        tools, handlers = assemble_tool_pool(workspace)

        # 一条新的 assistant 消息开始了。on_text 的实现方（如 ui.StreamPrinter）
        # 借这个信号重新打「BOT >>」前缀，否则工具日志和正文会连成一片。
        # 用可选方法而不是改回调签名：不关心边界的调用方（测试、脚本）传个
        # 普通函数就行。边界放在 loop 而不是 provider：所有 provider 共用一处，
        # 新增 provider 不会漏掉。
        _notify_stream(on_text, "begin_message")

        try:
            response = provider.chat(
                messages=messages,
                tools=tools,
                system=system_prompt,
                max_tokens=MAX_TOKENS,
                on_text=on_text,
            )
        except ProviderError as error:
            # providers 层已把 SDK 异常规范化（契约 4）。本轮无法继续，但必须
            # **优雅结束**：把异常抛穿会让 CLI 崩掉，连尚未保存的会话一起丢掉。
            # 只捕获 ProviderError —— 其它异常是代码缺陷，应当暴露出来。
            # 先收尾再报错：流式中断时屏幕上可能还有半行绿字
            _notify_stream(on_text, "end_message")
            _report_provider_error(error)
            return LoopOutcome(
                "provider_error", turns, total_tokens, detail=error.message
            )

        _notify_stream(on_text, "end_message")

        # 将 ContentBlock 转成字典（支持序列化）
        content_dict = [_block_to_dict(block) for block in response.content]
        messages.append({"role": "assistant", "content": content_dict})

        used = response.usage.input_tokens + response.usage.output_tokens
        total_tokens += used
        # 两个数不是一回事：total 是这轮对话的累计消耗（每轮重发历史，涨得快），
        # ctx 是本次请求真正发过去的输入大小（决定会不会触发压缩）。
        #
        # cache 是**唯一能证明前缀稳定的观测量**。前缀 = [tools][system][messages]，
        # 命中意味着这三段与上一次请求逐字节相同。第 2 轮起如果它一直是 0，
        # 说明有东西在变——按 tools 顺序 → system 里的动态内容 → 压缩 依次排查
        # （`run_prefix_audit.py` 可以不花一次 API 调用就查出是哪一段）。
        # 注意：0 也可能表示网关压根不上报，两种情况的区分办法见该脚本。
        cached = cached_prompt_tokens(response.usage)
        hit = f"{cached * 100 // response.usage.input_tokens}%" if response.usage.input_tokens else "-"
        print(
            f"{Color.GRAY}  [tokens] turn {turns}/{MAX_TURNS}: +{used:,} "
            f"(total: {total_tokens:,}, ctx: {response.usage.input_tokens:,}, "
            f"cache: {cached:,} / {hit}){Color.RESET}"
        )

        if total_tokens > TURN_TOKEN_BUDGET:
            print(
                f"{Color.RED}  [warn] 达到 token 预算上限 ({TURN_TOKEN_BUDGET})，"
                f"本轮未完成{Color.RESET}"
            )
            print(
                f"{Color.GRAY}  本轮累计 {total_tokens} tokens —— 每次请求都会重发整段历史，"
                f"所以这个值按次累加；当前上下文约 "
                f"{estimate_messages_tokens(messages)} tokens。{Color.RESET}"
            )
            print(
                f"{Color.GRAY}  调大 .env 的 TURN_TOKEN_BUDGET，或把任务拆成多轮提问。"
                f"{Color.RESET}"
            )
            # 这一轮的 assistant 消息已经写进 messages，里面可能带着
            # tool_use。直接 return 会留下没有 tool_result 的悬空调用，
            # 历史不合法且会被持久化到下一轮 —— 必须先补齐再退出。
            _answer_pending_tool_uses(
                messages,
                content_dict,
                "Error: 本轮 token 预算已耗尽，该工具调用被取消，未执行。",
            )
            return LoopOutcome(
                "budget",
                turns,
                total_tokens,
                detail=f"累计 {total_tokens} tokens 超过预算 {TURN_TOKEN_BUDGET}",
            )

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            # Stop hook：返回非 None 可强制续跑（对应教程 s04 的 Stop 语义）
            force = trigger_hooks("Stop", messages)
            if force:
                messages.append({"role": "user", "content": force})
                continue
            return LoopOutcome("completed", turns, total_tokens)

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

        # 连续失败 / 无进展的**策略**在 guards.py；这里只执行 loop 层面的动作。
        # 先查失败：一个持续报错的工具两个条件都会满足，先报「连续失败」
        # 比「无进展」更贴近模型能理解的原因。
        if failure_guard.should_abort:
            print(
                f"{Color.RED}  [error] 连续 {failure_guard.limit} 次工具调用失败，"
                f"本轮未完成{Color.RESET}"
            )
            print(
                f"{Color.GRAY}  连续失败多半是参数或路径不对；修正后可重新提问。"
                f"{Color.RESET}"
            )
            return LoopOutcome(
                "tool_failures",
                turns,
                total_tokens,
                detail=f"连续 {failure_guard.limit} 次工具调用返回 Error",
            )

        if progress_guard.should_abort:
            print(f"{Color.RED}  [error] 无进展：{progress_guard.detail}，本轮未完成"
                  f"{Color.RESET}")
            print(
                f"{Color.GRAY}  重复同样的调用不会得到新信息：换一种方法，"
                f"或直接说明卡在哪里。{Color.RESET}"
            )
            return LoopOutcome(
                "no_progress", turns, total_tokens, detail=progress_guard.detail
            )

        for feedback in (failure_guard.take_feedback(), progress_guard.take_feedback()):
            if not feedback:
                continue
            messages.append({"role": "user", "content": feedback})
            # 这条提示是给**模型**的，但用户也得知道发生过什么：否则他只看到
            # 同一轮被重复了三次、然后突然中止。只打第一行，完整内容留给模型。
            headline = feedback.splitlines()[0]
            if headline.startswith("[harness] "):
                headline = headline[len("[harness] "):]
            print(f"{Color.YELLOW}  [guard] {headline}{Color.RESET}")

    print(
        f"{Color.RED}  [warn] 达到最大轮次上限 ({MAX_TURNS})，本轮未完成{Color.RESET}"
    )
    print(
        f"{Color.GRAY}  该上限是兜底：正常任务应当先被「无进展」「连续失败」或 token "
        f"预算拦住。若确实需要更多轮，调大 .env 的 MAX_TURNS，或把任务拆成几步。"
        f"{Color.RESET}"
    )
    return LoopOutcome("max_turns", turns, total_tokens, detail=f"用满 {MAX_TURNS} 轮")
