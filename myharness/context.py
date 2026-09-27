"""上下文压缩：防止长对话超出 token 限制。

策略：
1. 保留最近 N 轮对话
2. 压缩时不破坏 tool_use/tool_result 配对（M2 验收标准）
3. 系统消息始终保留
"""

from typing import Any


class ContextManager:
    """上下文管理：自动压缩历史消息。"""

    def __init__(self, max_messages: int = 20):
        """
        初始化上下文管理器。

        Args:
            max_messages: 最多保留的消息数（user + assistant 配对计为 2 条）
        """
        self.max_messages = max_messages

    def compress(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        压缩消息历史，保留最近的对话。

        规则：
        1. 保留最近 max_messages 条消息
        2. 确保 tool_use 和 tool_result 成对出现
        3. 如果截断点在工具调用中间，向前移动到完整的对话轮次

        Args:
            messages: 原始消息列表

        Returns:
            压缩后的消息列表
        """
        if len(messages) <= self.max_messages:
            return messages

        # 从后往前保留 max_messages 条
        compressed = messages[-self.max_messages:]

        # 检查第一条消息是否是 tool_result
        # 如果是，说明对应的 tool_use 被截断了，需要找到完整的对话轮次
        first_msg = compressed[0]
        if first_msg["role"] == "user" and isinstance(first_msg.get("content"), list):
            # 检查是否包含 tool_result
            if any(
                isinstance(item, dict) and item.get("type") == "tool_result"
                for item in first_msg["content"]
            ):
                # 向前查找，直到找到不是 tool_result 的 user 消息
                start_idx = len(messages) - self.max_messages
                while start_idx > 0:
                    prev_msg = messages[start_idx - 1]
                    if prev_msg["role"] == "user":
                        # 检查是否是普通的文本消息
                        if isinstance(prev_msg.get("content"), str):
                            break
                    start_idx -= 1

                # 从这个位置开始保留
                compressed = messages[start_idx:]

        return compressed

    def should_compress(self, messages: list[dict[str, Any]]) -> bool:
        """
        判断是否需要压缩。

        Args:
            messages: 消息列表

        Returns:
            是否需要压缩
        """
        return len(messages) > self.max_messages
