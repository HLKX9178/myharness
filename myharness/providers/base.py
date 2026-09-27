"""Provider 基类：定义统一的模型调用接口。"""

from abc import ABC, abstractmethod
from typing import Any


# 全局注册表：provider名 → Provider类
PROVIDER_REGISTRY: dict[str, type["BaseProvider"]] = {}


class BaseProvider(ABC):
    """所有模型提供商的抽象基类。"""

    @classmethod
    def register(cls, name: str):
        """
        装饰器：将 Provider 类注册到全局注册表。

        用法：
            @BaseProvider.register("anthropic")
            class AnthropicProvider(BaseProvider):
                ...
        """
        def wrapper(provider_class):
            PROVIDER_REGISTRY[name] = provider_class
            return provider_class
        return wrapper

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
        max_tokens: int,
    ) -> Any:
        """
        发起一次对话请求。

        Args:
            messages: 对话历史（OpenAI 格式）
            tools: 工具列表（OpenAI 格式）
            system: 系统提示词
            max_tokens: 最大生成 token 数

        Returns:
            标准化的响应对象（包含 content, usage 等字段）
        """
        pass

    @abstractmethod
    def get_model_name(self) -> str:
        """返回当前使用的模型名称。"""
        pass
