"""OpenAI 兼容 Provider 实现（支持千问等兼容 OpenAI 格式的模型）。"""

import os
import json
from typing import Any

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
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


@BaseProvider.register("openai")
class OpenAIProvider(BaseProvider):
    """OpenAI 兼容模型提供商（支持 OpenAI、千问等）。"""

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
    ):
        """
        初始化 OpenAI Provider。

        Args:
            model: 模型名称（如 gpt-4o-mini、qwen-plus）
            api_key: API 密钥（默认从环境变量 OPENAI_API_KEY 读取）
            base_url: API 端点（默认 OpenAI，可改为千问等兼容端点）
            timeout: 单次请求超时秒数；None 时读 REQUEST_TIMEOUT_SECONDS
        """
        self.model = model
        self.request_timeout = resolve_request_timeout(timeout)
        self.client = OpenAI(
            api_key=api_key or os.getenv("OPENAI_API_KEY"),
            base_url=base_url or os.getenv("OPENAI_BASE_URL"),
            # 超时由本层显式设定，不用 SDK 默认的 600s（见 base.py 的说明）
            timeout=build_http_timeout(self.request_timeout, Timeout),
            # 重试归属本层（契约见 base.py 第 3 条）：关掉 SDK 自带重试，
            # 否则一次调用最坏会发 (2+1)×(2+1)=9 次真实请求
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
        调用 OpenAI Chat Completions API，并适配成 Anthropic 格式。

        Args:
            messages: 对话历史（Anthropic 格式）
            tools: 工具列表（Anthropic 格式，本方法内转成 OpenAI 格式）
            system: 系统提示词
            max_tokens: 最大生成 token 数

        Returns:
            带有 .content 属性的响应对象（适配成 Anthropic 格式）

        Raises:
            ProviderError: 调用失败（SDK 异常不会穿出本方法）
        """
        # 将 Anthropic 格式的 messages 转换成 OpenAI 格式
        openai_messages = self._convert_messages_to_openai(messages)

        # OpenAI 格式：system 作为第一条消息
        openai_messages = [{"role": "system", "content": system}] + openai_messages

        # 工具定义同样要转：漏掉这一步端点会直接 400
        # （'type' is a required property - 'tools.0'）
        openai_tools = self._convert_tools_to_openai(tools)

        response = self._with_retry(
            lambda: self.client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                tools=openai_tools or None,
                max_tokens=max_tokens,
            )
        )

        # 适配成 Anthropic 格式：将 OpenAI response 包装成带 .content 属性的对象
        return self._adapt_response(response)

    def get_model_name(self) -> str:
        """返回当前模型名称。"""
        return self.model

    def _classify_error(self, exc: BaseException) -> ProviderError:
        """把 openai SDK 的异常翻译成 ProviderError。

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
                hint="检查 .env 的 OPENAI_BASE_URL 与网络连通性（代理/防火墙）。",
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

    @staticmethod
    def _convert_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        将 Anthropic 格式的工具定义转换成 OpenAI function calling 格式。

        两种形态的差异：

            Anthropic  {"name": ..., "description": ..., "input_schema": {...}}
            OpenAI     {"type": "function",
                        "function": {"name": ..., "description": ..., "parameters": {...}}}

        漏掉这一步的后果不是「工具不可用」，而是端点直接 400：
        `'type' is a required property - 'tools.0'` —— 因为校验发生在
        模型与权限检查之前，报错信息完全指不到真正的起因。

        Args:
            tools: Anthropic 格式的工具列表

        Returns:
            OpenAI 格式的工具列表
        """
        converted = []
        for tool in tools:
            converted.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    # 无参数工具也必须给出合法的 object schema，不能省略
                    "parameters": tool.get("input_schema") or {
                        "type": "object",
                        "properties": {},
                    },
                },
            })
        return converted

    def _convert_messages_to_openai(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        将 Anthropic 格式的 messages 转换成 OpenAI 格式。

        Anthropic 格式:
            {"role": "user", "content": "text"}  或
            {"role": "user", "content": [{"type": "text", "text": "..."}, ...]}
            {"role": "assistant", "content": [{"type": "text", "text": "..."}, {"type": "tool_use", ...}]}

        OpenAI 格式:
            {"role": "user", "content": "text"}
            {"role": "assistant", "content": "text", "tool_calls": [...]}

        Args:
            messages: Anthropic 格式的消息列表

        Returns:
            OpenAI 格式的消息列表
        """
        openai_messages = []

        for msg in messages:
            role = msg["role"]
            content = msg["content"]

            # 1. 字符串内容（user 消息通常是这样）
            if isinstance(content, str):
                openai_messages.append({"role": role, "content": content})
                continue

            # 2. content 是列表（Anthropic 格式）
            if isinstance(content, list):
                # 提取文本和工具调用
                text_parts = []
                tool_calls = []

                for block in content:
                    # 字典格式（已序列化）
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
                        elif block.get("type") == "tool_use":
                            tool_calls.append({
                                "id": block["id"],
                                "type": "function",
                                "function": {
                                    "name": block["name"],
                                    "arguments": json.dumps(block["input"]),
                                },
                            })
                        elif block.get("type") == "tool_result":
                            # tool_result 转成 tool 消息
                            openai_messages.append({
                                "role": "tool",
                                "tool_call_id": block["tool_use_id"],
                                "content": block["content"],
                            })
                    # 对象格式（未序列化，兼容旧代码）
                    else:
                        if hasattr(block, 'type'):
                            if block.type == "text":
                                text_parts.append(block.text)
                            elif block.type == "tool_use":
                                tool_calls.append({
                                    "id": block.id,
                                    "type": "function",
                                    "function": {
                                        "name": block.name,
                                        "arguments": json.dumps(block.input),
                                    },
                                })

                # 构建 OpenAI 消息
                if role == "assistant":
                    msg_dict = {"role": "assistant"}
                    if text_parts:
                        msg_dict["content"] = " ".join(text_parts)
                    else:
                        msg_dict["content"] = None
                    if tool_calls:
                        msg_dict["tool_calls"] = tool_calls
                    openai_messages.append(msg_dict)
                elif role == "user" and text_parts:
                    openai_messages.append({"role": "user", "content": " ".join(text_parts)})

        return openai_messages

    def _adapt_response(self, response: Any) -> Any:
        """
        将 OpenAI 响应适配成 Anthropic 格式。

        OpenAI 格式：
            response.choices[0].message.content: str
            response.choices[0].message.tool_calls: list[ToolCall]

        Anthropic 格式：
            response.content: list[Block]，Block 有 .type 属性
                - TextBlock: type="text", text=...
                - ToolUseBlock: type="tool_use", id=..., name=..., input=...

        Args:
            response: OpenAI ChatCompletion 对象

        Returns:
            带有 .content 属性的适配对象
        """
        message = response.choices[0].message
        content_blocks = []

        # 1. 文本内容
        if message.content:
            # 模拟 Anthropic TextBlock（带 model_dump 方法）
            class TextBlock:
                type = "text"
                def __init__(self, text: str):
                    self.text = text
                def model_dump(self):
                    return {"type": "text", "text": self.text}
            content_blocks.append(TextBlock(message.content))

        # 2. 工具调用
        if message.tool_calls:
            for tool_call in message.tool_calls:
                class ToolUseBlock:
                    type = "tool_use"
                    def __init__(self, id: str, name: str, input: dict):
                        self.id = id
                        self.name = name
                        self.input = input
                    def model_dump(self):
                        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}

                content_blocks.append(
                    ToolUseBlock(
                        id=tool_call.id,
                        name=tool_call.function.name,
                        input=json.loads(tool_call.function.arguments),
                    )
                )

        # 3. 使用情况
        class Usage:
            def __init__(self, input_tokens: int, output_tokens: int):
                self.input_tokens = input_tokens
                self.output_tokens = output_tokens

        usage = Usage(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
        )

        # 创建适配对象
        class AdaptedResponse:
            def __init__(self, content: list, usage: Usage):
                self.content = content
                self.usage = usage

        return AdaptedResponse(content_blocks, usage)
