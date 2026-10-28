"""OpenAI 兼容 Provider 实现（支持千问等兼容 OpenAI 格式的模型）。"""

import os
import json
from types import SimpleNamespace
from typing import Any, Callable

from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    BadRequestError,
    OpenAI,
    Timeout,
)

from myharness.context import estimate_messages_tokens, estimate_tokens
from myharness.providers.base import (
    BaseProvider,
    ProviderError,
    build_http_timeout,
    cached_prompt_tokens,
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
        # 端点不支持 stream_options 时只提醒一次：否则每轮都刷一遍
        self._warned_missing_usage = False
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
        on_text: Callable[[str], None] | None = None,
    ) -> Any:
        """
        调用 OpenAI Chat Completions API，并适配成 Anthropic 格式。

        Args:
            messages: 对话历史（Anthropic 格式）
            tools: 工具列表（Anthropic 格式，本方法内转成 OpenAI 格式）
            system: 系统提示词
            max_tokens: 最大生成 token 数
            on_text: 文本分片回调；None 表示不流式（返回值完全相同）

        Returns:
            带有 .content 属性的响应对象（适配成 Anthropic 格式）

        Raises:
            ProviderError: 调用或响应解析失败（SDK 异常与 JSON 解析错误都不会穿出本方法）
        """
        # 将 Anthropic 格式的 messages 转换成 OpenAI 格式
        openai_messages = self._convert_messages_to_openai(messages)

        # OpenAI 格式：system 作为第一条消息
        openai_messages = [{"role": "system", "content": system}] + openai_messages

        # 工具定义同样要转：漏掉这一步端点会直接 400
        # （'type' is a required property - 'tools.0'）
        openai_tools = self._convert_tools_to_openai(tools)

        def attempt():
            """一次完整的尝试：请求 + 适配。

            适配必须留在重试**内部**。它原先在 `_with_retry` 之外，于是
            `json.loads(tool_call.function.arguments)` 在 MAX_TOKENS 截断时抛出的
            JSONDecodeError 会绕过所有归一逻辑直接穿到 `agent_loop`（那里只捕获
            ProviderError），CLI 带着 traceback 崩掉、会话一起丢失。
            放进来的另一个好处是：适配期的任何异常都会被 `_classify_error`
            兜底成 ProviderError，不存在「某条支路忘了翻译」的漏网。
            """
            if on_text is not None:
                # 流式分支自己发请求（要先试 stream_options 再降级），
                # 但最终仍然返回同一个适配后的响应对象
                return self._stream_response(
                    on_text, openai_messages, openai_tools, max_tokens
                )

            response = self.client.chat.completions.create(
                model=self.model,
                messages=openai_messages,
                tools=openai_tools or None,
                max_tokens=max_tokens,
            )
            return self._adapt_response(response)

        return self._with_retry(attempt)

    def _stream_response(
        self,
        on_text: Callable[[str], None],
        openai_messages: list[dict[str, Any]],
        openai_tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> Any:
        """流式请求，并把分片拼回与非流式**完全一致**的响应对象。

        两个必须自己处理的点：

        1. **分片累加**。文本是逐块来的；tool_call 的参数更是按 `index` 分片、
           可能拆在十几个 chunk 里。拼装必须按 index 归并，拼完才能交给
           `_parse_tool_arguments`（截断检测也因此照常生效）。
        2. **流中断不能重试**。一旦有文字已经交给 `on_text`（用户已经看见），
           重试就会把同样的内容打第二遍。这里的做法是：首片之前抛原异常，交给
           `_with_retry` 按可重试错误处理；首片之后立刻转成不可重试的
           ProviderError，让本轮干净结束。
        """
        emitted = False
        usage = None
        finish_reason = None
        text_parts: list[str] = []
        calls: dict[int, dict[str, str]] = {}

        def emit(text: str | None) -> None:
            nonlocal emitted
            if not text:
                return
            emitted = True
            text_parts.append(text)
            on_text(text)

        try:
            stream = self._open_stream(openai_messages, openai_tools, max_tokens)
            for chunk in stream:
                if getattr(chunk, "usage", None) is not None:
                    usage = chunk.usage

                for choice in getattr(chunk, "choices", None) or []:
                    delta = getattr(choice, "delta", None)
                    if delta is not None:
                        emit(getattr(delta, "content", None))
                        for fragment in getattr(delta, "tool_calls", None) or []:
                            # 个别兼容端点不给 index；缺省按 0 处理，
                            # 否则 sorted() 会在 None 与 int 之间比较报错
                            index = getattr(fragment, "index", None)
                            slot = calls.setdefault(
                                index if index is not None else 0,
                                {"id": "", "name": "", "arguments": ""},
                            )
                            if getattr(fragment, "id", None):
                                slot["id"] = fragment.id
                            function = getattr(fragment, "function", None)
                            if function is not None:
                                if getattr(function, "name", None):
                                    slot["name"] = function.name
                                if getattr(function, "arguments", None):
                                    slot["arguments"] += function.arguments

                    if getattr(choice, "finish_reason", None):
                        finish_reason = choice.finish_reason
        except Exception as exc:
            if emitted:
                raise ProviderError(
                    f"流式输出中断：{type(exc).__name__}: {exc}",
                    retryable=False,  # 已经打到屏幕上的字收不回来，重试只会重复
                    hint="本轮已放弃（不完整的回复不会进历史），重新提问即可。",
                ) from exc
            raise

        if usage is None:
            usage = self._estimate_usage(openai_messages, text_parts, calls)

        tool_calls = [
            SimpleNamespace(
                id=slot["id"] or f"call_{index}",
                type="function",
                function=SimpleNamespace(
                    name=slot["name"], arguments=slot["arguments"]
                ),
            )
            for index, slot in sorted(calls.items())
        ] or None

        assembled = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="".join(text_parts) or None,
                        tool_calls=tool_calls,
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=usage,
        )
        return self._adapt_response(assembled)

    def _open_stream(
        self,
        openai_messages: list[dict[str, Any]],
        openai_tools: list[dict[str, Any]],
        max_tokens: int,
    ) -> Any:
        """建立流式请求；端点不认 stream_options 时自动降级。"""
        kwargs = dict(
            model=self.model,
            messages=openai_messages,
            tools=openai_tools or None,
            max_tokens=max_tokens,
            stream=True,
        )
        try:
            return self.client.chat.completions.create(
                **kwargs, stream_options={"include_usage": True}
            )
        except BadRequestError:
            # 部分兼容端点直接 400 拒绝 stream_options。缺了它只是拿不到 usage，
            # 不该让整轮失败 —— 退回普通流，代价见 _estimate_usage。
            return self.client.chat.completions.create(**kwargs)

    def _estimate_usage(
        self,
        openai_messages: list[dict[str, Any]],
        text_parts: list[str],
        calls: dict[int, dict[str, str]],
    ) -> Any:
        """端点没给 usage 时的估算，并提醒一次。

        不静默返回 0：`TURN_TOKEN_BUDGET`（累计消耗）与 `MAX_CONTEXT_TOKENS`
        （上下文压缩）都依赖 usage，默默失效等于两道护栏一起消失。
        估算复用 context 里的启发式函数，避免两处口径不一致。
        """
        if not self._warned_missing_usage:
            print(
                "[provider] 端点未返回 usage（可能不支持 stream_options），"
                "token 预算按字符数估算"
            )
            self._warned_missing_usage = True

        output = "".join(text_parts) + "".join(
            slot["arguments"] for slot in calls.values()
        )
        return SimpleNamespace(
            prompt_tokens=max(1, estimate_messages_tokens(openai_messages)),
            completion_tokens=max(1, estimate_tokens(output)),
            # 估出来的数字不含缓存信息，显式给 0：让 loop 侧统一按「未上报」处理，
            # 而不是把一个猜出来的值混进命中率统计里。
            prompt_tokens_details=None,
        )

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

    @staticmethod
    def _parse_tool_arguments(tool_call: Any, *, truncated: bool) -> dict[str, Any]:
        """
        把 ``tool_call.function.arguments`` 解析成 dict。

        SDK 给的是**字符串**（JSON 文本），不是对象；而 loop 与 dispatch 都要求
        `input` 是 dict（dispatch 用 `handler(**block.input)` 调用）。解析失败
        必须在这里变成可诊断的 ProviderError，不能把 JSONDecodeError 放出去。

        Args:
            tool_call: OpenAI 的 tool_call 对象
            truncated: 本次响应的 finish_reason 是否为 length

        Returns:
            参数 dict（无参数时为空 dict）

        Raises:
            ProviderError: 参数不是合法 JSON，或不是 JSON 对象
        """
        raw = tool_call.function.arguments
        name = tool_call.function.name

        # 部分端点在无参数工具上给空字符串/None，这不是错误
        if raw is None or not str(raw).strip():
            return {}

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            if truncated:
                raise ProviderError(
                    f"工具调用参数被截断（{name}）：模型输出触到了 max_tokens，"
                    f"arguments 在第 {exc.pos} 个字符处断开，不是合法 JSON",
                    retryable=False,  # 同样的 max_tokens 重发只会再截一次
                    code="invalid_tool_arguments",
                    hint=(
                        "调大 .env 的 MAX_TOKENS，或让模型把大文件拆成多次 "
                        "write_file / edit_file 分批写。"
                    ),
                ) from exc

            raise ProviderError(
                f"工具调用参数不是合法 JSON（{name}）：{exc.msg}（第 {exc.pos} 个字符）",
                retryable=True,  # 未截断的坏 JSON 多为随机翻车，重试可能成功
                code="invalid_tool_arguments",
                hint=(
                    "已按退避重试；持续失败可调大 MAX_TOKENS，"
                    "或换用 function calling 更稳定的模型。"
                ),
            ) from exc

        if not isinstance(parsed, dict):
            raise ProviderError(
                f"工具调用参数不是 JSON 对象（{name}）：得到 {type(parsed).__name__}",
                retryable=not truncated,
                code="invalid_tool_arguments",
                hint="工具参数必须是 JSON 对象，请让模型按 schema 重新生成。",
            )

        return parsed

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
        # "length" 表示输出撞到了 max_tokens：此时 arguments 很可能被切断，
        # 解析失败要按「截断」解释，而不是含糊地说「不是合法 JSON」
        truncated = getattr(response.choices[0], "finish_reason", None) == "length"
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
                        input=self._parse_tool_arguments(tool_call, truncated=truncated),
                    )
                )

        # 3. 使用情况
        #
        # cached_tokens 是**前缀缓存命中**的证据：它由网关按「与本请求完全
        # 相同的最大前缀」计算，模型无法伪造。缺了它，缓存有没有生效就只能
        # 靠猜——而前缀稳不稳定，恰恰是 Harness 层最容易做错的一件事
        # （system 里放动态内容、工具池顺序抖动，都会让整段历史作废）。
        class Usage:
            def __init__(self, input_tokens: int, output_tokens: int, cached_tokens: int = 0):
                self.input_tokens = input_tokens
                self.output_tokens = output_tokens
                self.cached_tokens = cached_tokens

        usage = Usage(
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            cached_tokens=cached_prompt_tokens(response.usage),
        )

        # 创建适配对象
        class AdaptedResponse:
            def __init__(self, content: list, usage: Usage):
                self.content = content
                self.usage = usage

        return AdaptedResponse(content_blocks, usage)
