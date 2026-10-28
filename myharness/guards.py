"""工具调用守卫：以 hook 形式实现的调用策略。

对应 AGENTS.md 契约 6 —— 策略挂在 hook 上，机制留在 loop。

三个守卫，管三件不同的事：

  RepeatCallGuard          PreToolUse   「你刚做过一模一样的」——即时、便宜、无状态窗口
  ConsecutiveFailureGuard  PostToolUse  「你一直在失败」——不看指纹，遇成功清零
  ProgressGuard            PostToolUse  「你一直在得到同样的结果」——看指纹 + 看结果

前两个是**动作**层面的判断，第三个是**结果**层面的判断。三次一成一败的交替
调用骗得过失败计数，也骗不过结果指纹：A 的结果每次都一样，就是没进展。

关于状态：这三个守卫是**有状态的 hook**。跨轮状态由 agent_loop 在
每次对话轮次开始时通过 reset_turn_state() 清空 —— 否则用户第二遍问
同样的问题时，RepeatCallGuard 会误拦、ProgressGuard 会误判。
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

# 权限策略拒绝的文案前缀。与 security.permission_hook 的返回字符串耦合：
# 契约 1 要求工具边界只返回字符串，没有结构化标记可用，只能靠前缀识别。
# 改动任一侧都必须同步这里。
PERMANENT_DENIAL_PREFIXES = ("Error: Permission denied",)


def is_permanent_denial(output: str) -> bool:
    """是否为「策略拒绝」（路径越界之类）。

    这类结果的共同点：参数没有写错，**重试永远不会成功**。所以「连续失败
    中止」和「无进展中止」这两种针对尝试次数的策略对它都不适用 ——
    否则模型连试几次同一个越界路径，整轮对话就会被顶到阈值而终止，
    而它缺的其实只是一个合法路径。
    """
    return str(output).lstrip().startswith(PERMANENT_DENIAL_PREFIXES)


class RepeatCallGuard:
    """PreToolUse：拦截参数完全相同的连续重复调用。

    完全迁移自 agent_loop，并顺带修掉一个协议问题：

        原实现是「跳过本轮工具执行 + 追加一条 user 文本消息」，
        这会让 assistant 的 tool_use 缺少配对的 tool_result。

    现在改为由 hook 返回拦截消息 —— 它本身就是该 tool_use 对应的
    tool_result，消息协议始终合法。
    """

    def __init__(self) -> None:
        self._last: tuple[str, str] | None = None

    def __call__(self, block: Any) -> str | None:
        current = (block.name, str(sorted((block.input or {}).items())))

        if current == self._last:
            self._last = None  # 只拦一次，避免永久锁死
            return (
                "你刚才已经用相同参数调用过这个工具了。"
                "如果还需要信息，请换一个方法或调整参数。"
            )

        self._last = current
        return None

    def reset(self) -> None:
        self._last = None


class ConsecutiveFailureGuard:
    """PostToolUse：统计连续失败的工具调用。

    只负责**策略** —— 什么算失败、阈值多少、何时告警。
    「终止循环」是 loop 的机制，由 loop 读取 should_abort 后执行。

    语义说明：原实现按「一轮内全部工具失败」计数，现在按「连续失败的
    单个工具调用」计数。也就是说，如果一轮里有 3 个工具全部失败，
    原实现记 1 次，现在记 3 次 —— 触发更早。阈值可通过构造参数调整。
    """

    def __init__(self, limit: int = 3, warn_after: int = 2) -> None:
        self.limit = limit
        self.warn_after = warn_after
        self.failures = 0
        self._feedback: str | None = None

    def __call__(self, block: Any, output: str) -> None:
        # 策略拒绝不计次：它不是「尝试失败」，而是「这条路根本不通」。
        # 计入阈值会让一次越界 + 两次重试就把整轮对话掐掉。
        if is_permanent_denial(output):
            self._feedback = (
                "上一个调用被权限策略拒绝，这不是参数写错 —— 重复同一路径不会成功。"
                "请改用当前工作区内的路径；如果确实需要写这个目录，"
                "请让用户用 --workspace 指定该目录后重新开始会话。"
            )
            return None

        if str(output).startswith("Error:"):
            self.failures += 1
            if self.failures == self.warn_after:
                self._feedback = (
                    "连续工具调用失败。请检查参数格式、路径是否正确，"
                    "或尝试换一种方法。"
                )
        else:
            self.failures = 0
            self._feedback = None
        return None

    @property
    def should_abort(self) -> bool:
        return self.failures >= self.limit

    def take_feedback(self) -> str | None:
        """取出待注入的反馈消息（取一次即清空）。"""
        feedback, self._feedback = self._feedback, None
        return feedback

    def reset(self) -> None:
        self.failures = 0
        self._feedback = None


# 只比较结果的前若干字符：模型读一个大文件时，末尾差异通常是噪声，
# 真正说明「有没有拿到新信息」的是开头。同时也把哈希成本钉死。
_RESULT_HEAD = 4000

# 参数摘要写进反馈时截断到多少字符 —— 太长会挤占模型注意力
_PARAM_HEAD = 200


def _canonical_json(obj: Any) -> str:
    """稳定序列化：键排序 + 保留中文 + 无法序列化时转字符串。

    不能退回 ``str(sorted(items))``：嵌套 dict 的键序不稳定，同一组参数会
    算出不同指纹，无进展检测就永远抓不到东西。
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)


def _result_digest(output: str) -> str:
    """结果指纹：去掉首尾空白后取前 4000 字符做 sha1。"""
    return hashlib.sha1(
        str(output).strip()[:_RESULT_HEAD].encode("utf-8", errors="replace")
    ).hexdigest()


@dataclass
class _Record:
    """单个「工具 + 参数」组合的调用记录。"""

    seen: int = 0                # 该指纹被调用的总次数
    last_result_hash: str = ""   # 上一次结果的指纹
    unchanged: int = 0           # 结果连续相同的次数（首次出现即计 1）
    warned: bool = False         # 同一段停顿只警告一次


class ProgressGuard:
    """PostToolUse：按「工具 + 参数」指纹检测原地打转。

    为什么需要它：轮次是**动作数，不是进度**。80 轮的正常重构和 80 轮的
    原地打转在轮次维度上完全一样，所以把 MAX_TURNS 调大之前，得先有一层
    看结果的刹车。

    为什么在 PostToolUse：无进展判定必须**看结果**，PreToolUse 拿不到输出。
    RepeatCallGuard 管的是「你刚做过同样的」，即时、便宜；这里管的是
    「你一直没进展」，需要窗口和结果。两者互补，不合并。

    **轮询豁免是自然给出的**：后台任务查询这类调用参数相同但结果在变，
    ``unchanged`` 会一直是 1，永远不会触发。不要退回「只比参数」的旧逻辑
    —— 那会把「等任务跑完」误判成死循环。

    已知局限（fail-open，可接受）：输出含时间戳或随机数时指纹每次都不同，
    检测会放行。不做数字/时间戳归一化 —— 那会误伤真实进展，这类漏网由
    token 预算兜底。
    """

    def __init__(self, warn_after: int = 3, abort_after: int = 5) -> None:
        if abort_after <= warn_after:
            raise ValueError("abort_after 必须大于 warn_after")
        self.warn_after = warn_after
        self.abort_after = abort_after
        self._records: dict[str, _Record] = {}
        self._feedback: str | None = None
        self._abort_detail: str | None = None

    def __call__(self, block: Any, output: str) -> None:
        # 策略拒绝不是「无进展」：同一条越界路径本来就不该再试第二次，
        # 把它计入 unchanged 只会让进度守卫误判成原地打转并中止。
        if is_permanent_denial(output):
            return None

        fingerprint = f"{block.name}\x00{_canonical_json(getattr(block, 'input', None) or {})}"
        digest = _result_digest(output)

        record = self._records.get(fingerprint)
        if record is None:
            # 首次出现就计 1：`unchanged` 的语义是「相同结果已经出现几次」，
            # 不是「比上一次多了几次」。否则 warn_after=3 要第 4 次才触发，
            # 与「连续 3 次返回完全一致」的文案对不上。
            self._records[fingerprint] = _Record(
                seen=1, last_result_hash=digest, unchanged=1
            )
            return None

        record.seen += 1
        if digest == record.last_result_hash:
            record.unchanged += 1
        else:
            # 拿到新结果说明还有进展：重新计数，并允许下次停顿再次提醒
            record.last_result_hash = digest
            record.unchanged = 1
            record.warned = False

        if record.unchanged >= self.abort_after:
            if self._abort_detail is None:
                self._abort_detail = (
                    f"`{block.name}` 用相同参数连续 {record.unchanged} 次"
                    f"得到完全相同的返回"
                )
        elif record.unchanged >= self.warn_after and not record.warned:
            record.warned = True
            self._feedback = self._build_feedback(block, record.unchanged)
        return None

    @staticmethod
    def _build_feedback(block: Any, count: int) -> str:
        """喂回给模型的停顿提示。

        必须包含模型自己算不出来的事实（次数、参数摘要），否则等于把它
        刚看到的那条结果再复述一遍，不注入任何新信息。
        """
        params = _canonical_json(getattr(block, "input", None) or {})
        if len(params) > _PARAM_HEAD:
            params = params[:_PARAM_HEAD] + "…（已截断）"
        return (
            f"[harness] 无进展检测：`{block.name}` 已用相同参数连续调用 "
            f"{count} 次，返回内容完全一致。\n"
            f"参数摘要：{params}\n"
            f"继续重复不会得到新信息。请三选一：\n"
            f"(1) 换一种方法获取信息；(2) 直接说明当前卡在哪里；"
            f"(3) 若确需再读，说明这次要确认什么。"
        )

    @property
    def should_abort(self) -> bool:
        return self._abort_detail is not None

    @property
    def detail(self) -> str:
        """终止原因（供 LoopOutcome.detail 使用）。"""
        return self._abort_detail or ""

    def take_feedback(self) -> str | None:
        """取出待注入的反馈消息（取一次即清空）。"""
        feedback, self._feedback = self._feedback, None
        return feedback

    def reset(self) -> None:
        self._records.clear()
        self._feedback = None
        self._abort_detail = None


# hooks 需要可调用对象，loop 需要查询状态，因此在这里持有实例。
# 这是 AGENTS.md「禁止可变全局状态」的**受控例外**：状态是本轮对话内的，
# 且必须由 agent_loop 在每轮开始时清空。
repeat_call_guard = RepeatCallGuard()
failure_guard = ConsecutiveFailureGuard()
progress_guard = ProgressGuard()


def reset_turn_state() -> None:
    """清空守卫的跨轮状态。由 agent_loop 在每轮对话开始时调用。"""
    repeat_call_guard.reset()
    failure_guard.reset()
    progress_guard.reset()
