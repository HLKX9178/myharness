"""上下文压缩：控制单次请求的输入规模。

两条独立的触发线：

1. **消息条数**（兜底）：短消息攒多了也会变大，但条数不是真正的尺度；
2. **估算 token 数**（主判据）：一条 4 万字符的 tool_result 抵得上几十条闲聊，
   按条数压缩会放过它，按 token 才会动手。

压缩不得破坏 tool_use / tool_result 配对（契约 2），所以切点只能落在
「用户提出新请求」的位置，不能随手切最后 N 条。
"""

import json
import re
from typing import Any

# CJK 字符按 1 字符 ≈ 1 token 估算（略偏保守：宁可早一点压缩，
# 也不要让请求真的超窗）；其余字符按 3.5 字符 ≈ 1 token。
_ASCII_CHARS_PER_TOKEN = 3.5
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数。

    只用于预算判断，不对外展示。不需要精确：真值由 provider 的 usage 给出，
    这里要的是「别把 30 万字符的历史当成 3 千 tokens」。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + int(other / _ASCII_CHARS_PER_TOKEN) + 1


def estimate_message_tokens(message: dict[str, Any]) -> int:
    """估算单条消息的 token 数（含 role/type 等结构开销）。"""
    return estimate_tokens(json.dumps(message, ensure_ascii=False))


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    """估算整段历史的 token 数。"""
    return sum(estimate_message_tokens(message) for message in messages)


def _is_user_text(message: dict[str, Any]) -> bool:
    """是否是「用户提出请求」的那条消息（不是 tool_result 回填）。"""
    return message.get("role") == "user" and isinstance(message.get("content"), str)


class ContextManager:
    """上下文管理：按条数与 token 估算压缩历史。"""

    def __init__(
        self,
        max_messages: int = 200,
        max_tokens: int | None = None,
        trigger_ratio: float = 0.8,
        target_ratio: float = 0.5,
        max_result_chars: int = 20_000,
    ):
        """
        Args:
            max_messages: 最多保留的消息数（很松的兜底线，正常由 max_tokens 先触发）
            max_tokens: 单次请求的输入上限；None 表示只按条数压缩
            trigger_ratio: 估算值超过 max_tokens 的这个比例就压缩
            target_ratio: 压缩到 max_tokens 的这个比例以下
            max_result_chars: 单条 tool_result 最多保留的字符数（仅在仍然超限时生效）
        """
        self.max_messages = max_messages
        self.max_tokens = max_tokens
        self.trigger_ratio = trigger_ratio
        self.target_ratio = target_ratio
        self.max_result_chars = max_result_chars

    # ---------- 对外接口 ----------

    def estimate(self, messages: list[dict[str, Any]]) -> int:
        """估算当前历史的 token 数。"""
        return estimate_messages_tokens(messages)

    def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        """是否需要压缩：条数或 token 任一超线。"""
        if self._count_cut(messages) is not None:
            return True
        if self.max_tokens:
            return self.estimate(messages) > self.max_tokens * self.trigger_ratio
        return False

    def compress(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """压缩历史，返回新列表（不修改传入的对象）。

        顺序：
        1. 按 token 丢最早的整轮对话；
        2. 仍超过条数上限时再按条数收一次；
        3. 如果一条超大的 tool_result 自己就把预算吃满，截断它。
        """
        if not messages or not self.should_compress(messages):
            return messages

        compressed = list(messages)

        if self.max_tokens:
            compressed = self._trim_to_tokens(compressed)

        if len(compressed) > self.max_messages:
            compressed = self._trim_to_count(compressed)

        if self.max_tokens and self.estimate(compressed) > self.max_tokens * self.trigger_ratio:
            compressed = self._truncate_results(compressed)

        return compressed

    # ---------- 内部实现 ----------

    def _trim_to_tokens(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """丢掉最早的整轮对话，直到估算值落进 target。"""
        counts = [estimate_message_tokens(message) for message in messages]
        target = int(self.max_tokens * self.target_ratio)

        # 后缀和：suffix[i] 表示 messages[i:] 的估算 token 数
        suffix = [0] * (len(messages) + 1)
        for index in range(len(messages) - 1, -1, -1):
            suffix[index] = suffix[index + 1] + counts[index]

        if suffix[0] <= target:
            return messages

        candidates = [i for i, message in enumerate(messages) if _is_user_text(message)]

        # 优先选「最早的、仍能装进预算」的那一轮用户请求：既能丢掉旧内容，
        # 又不会把当前这轮的任务描述切掉。
        start = candidates[-1] if candidates else 0
        for index in candidates:
            if suffix[index] <= target:
                start = index
                break

        if not candidates:
            # 没有可用的轮次边界（历史以 tool_result 开头）：只能逐条丢，
            # 最后靠 _truncate_results 兜底。
            while start < len(messages) - 1 and suffix[start] > target:
                start += 1

        return messages[start:]

    def _trim_to_count(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """按条数保留最近的消息，切点必须落在用户请求上。"""
        cut = self._count_cut(messages)
        return messages if cut is None else messages[cut:]

    def _count_cut(self, messages: list[dict[str, Any]]) -> int | None:
        """找一个能切的位置：用户请求，且切完不超过 max_messages。

        切点只能往后找 —— 往前回退到上一条用户请求等于什么都没切，会出现
        「每轮都报压缩、实际一条没少」的空转（实测踩过）。找不到就返回 None：
        当前这一轮的工具链还在进行中，从中间截断会拆散 tool_use/tool_result。
        """
        if len(messages) <= self.max_messages:
            return None

        cut = len(messages) - self.max_messages
        for index in range(cut, len(messages)):
            if _is_user_text(messages[index]):
                return index
        return None

    def _truncate_results(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """截断超大的 tool_result —— 单条结果就吃满预算时的最后手段。"""
        budget = int(self.max_tokens * self.trigger_ratio)
        trimmed = list(messages)

        for index, message in enumerate(trimmed):
            content = message.get("content")
            if not isinstance(content, list):
                continue

            blocks = []
            changed = False
            for block in content:
                text = block.get("content") if isinstance(block, dict) else None
                if (
                    isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and isinstance(text, str)
                    and len(text) > self.max_result_chars
                ):
                    # 复制一份再改：这条消息同时躺在调用方的历史里（会被存盘）
                    new_block = dict(block)
                    new_block["content"] = (
                        text[: self.max_result_chars]
                        + f"\n...[已截断，原 {len(text)} 字符，只保留前 "
                        f"{self.max_result_chars} 字符]"
                    )
                    blocks.append(new_block)
                    changed = True
                else:
                    blocks.append(block)

            if changed:
                new_message = dict(message)
                new_message["content"] = blocks
                trimmed[index] = new_message
                if self.estimate(trimmed) <= budget:
                    break

        return trimmed
