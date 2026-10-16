"""Anthropic Claude Provider 实现。"""

import os
from typing import Any

from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    Anthropic,
    Timeout,
)

from myharness.providers.base import (
    BaseProvider,
    ProviderError,
    build_http_timeout,
    extract_error_detail,
    hint_for_status,
    is_retryable_status,
    parse_retry_after,
    resolve_request_timeout,
)


@BaseProvider.register("anthropic")
class AnthropicProvider(BaseProvider):
    """Anthropic Claude 模型提供商。"""

    def __init__(
        self,
        model: str = "claude-3-5-sonnet-20241022",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ):
        """
        初始化 Anthropic Provider。

        Args:
            model: 模型名称（默认 Claude 3.5 Sonnet）
            api_key: API 密钥（默认从环境变量读取）
            base_url: 自定义端点（如智谱 GLM）；None 时用官方端点。
                      做成参数而不是让调用方替换 client —— 否则这条支路会
                      悄悄丢掉超时与 max_retries 设置。
            timeout: 单次请求超时秒数；None 时读 REQUEST_TIMEOUT_SECONDS
        """
        self.model = model
        self.request_timeout = resolve_request_timeout(timeout)
        self.client = Anthropic(
            api_key=api_key or os.getenv("ANTHROPIC_API_KEY"),
            base_url=base_url,
            # 超时由本层显式设定，不用 SDK 默认的 600s（见 base.py 的说明）
            timeout=build_http_timeout(self.request_timeout, Timeout),
            # 重试归属本层（契约见 base.py 第 3 条）
            max_retries=0,
        )

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

        Raises:
            ProviderError: 调用失败（SDK 异常不会穿出本方法）
        """
        return self._with_retry(
            lambda: self.client.messages.create(
                model=self.model,
                system=system,
                max_tokens=max_tokens,
                messages=messages,
                tools=tools,
            )
        )

    def get_model_name(self) -> str:
        """返回当前模型名称。"""
        return self.model

    def _classify_error(self, exc: BaseException) -> ProviderError:
        """把 anthropic SDK 的异常翻译成 ProviderError。

        顺序有讲究：APITimeoutError 是 APIConnectionError 的子类，
        必须先判断超时，否则超时会被描述成「连接失败」。
        """
        if isinstance(exc, APITimeoutError):
            return ProviderError(
                "请求超时",
                retryable=True,
                hint="服务端未在超时时间内响应。已按退避重试；持续超时可检查网络或调小 MAX_TOKENS。",
            )

        if isinstance(exc, APIConnectionError):
            return ProviderError(
                f"无法连接模型端点 {self.client.base_url}",
                retryable=True,
                hint="检查 .env 的 ANTHROPIC_BASE_URL 与网络连通性（代理/防火墙）。",
            )

        if isinstance(exc, APIStatusError):
            code, detail = extract_error_detail(exc.body)
            label = f"HTTP {exc.status_code}"
            if code:
                label += f" {code}"
            return ProviderError(
                f"{label}: {detail}" if detail else label,
                retryable=is_retryable_status(exc.status_code),
                status_code=exc.status_code,
                code=code,
                hint=hint_for_status(exc.status_code),
                retry_after=parse_retry_after(exc),
            )

        # 认不出来的一律不可重试：可能是编程错误，不该被当成网络抖动掩盖
        return ProviderError(f"{type(exc).__name__}: {exc}", retryable=False)
