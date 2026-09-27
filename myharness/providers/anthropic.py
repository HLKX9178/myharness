"""Anthropic Claude Provider 实现。"""

import os
from typing import Any
from anthropic import Anthropic

from myharness.providers.base import BaseProvider


@BaseProvider.register("anthropic")
class AnthropicProvider(BaseProvider):
    """Anthropic Claude 模型提供商。"""

    def __init__(self, model: str = "claude-3-5-sonnet-20241022", api_key: str | None = None):
        """
        初始化 Anthropic Provider。

        Args:
            model: 模型名称（默认 Claude 3.5 Sonnet）
            api_key: API 密钥（默认从环境变量读取）
        """
        self.model = model
        self.client = Anthropic(api_key=api_key or os.getenv("ANTHROPIC_API_KEY"))

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
        max_tokens: int,
    ) -> Any:
        """
        调用 Anthropic Messages API。

        Returns:
            Anthropic Response 对象（包含 content, usage 等）
        """
        return self.client.messages.create(
            model=self.model,
            system=system,
            max_tokens=max_tokens,
            messages=messages,
            tools=tools,
        )

    def get_model_name(self) -> str:
        """返回当前模型名称。"""
        return self.model
