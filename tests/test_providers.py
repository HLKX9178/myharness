"""Provider 错误规范化与重试的测试。

回归的真实故障：模型端点返回 403（`AccessDenied.Unpurchased`）时，SDK 异常
直接抛穿 `agent_loop` 和 CLI —— 程序带着 traceback 崩掉，尚未保存的会话一起
丢失。按 AGENTS.md 契约 4「错误是数据，不是异常」，这不该发生。

这里锁住三件事：

  1. SDK 异常在 provider 层被翻译成 ProviderError，不穿出去
  2. 只有可重试的错误才重试，且重试次数有上界
  3. agent_loop 遇到 ProviderError 时优雅结束，并保持 messages 干净
"""

from types import SimpleNamespace

import anthropic
import openai
import pytest

httpx2 = pytest.importorskip("httpx2")

from myharness import hooks, loop  # noqa: E402
from myharness.providers import base  # noqa: E402
from myharness.providers.anthropic import AnthropicProvider  # noqa: E402
from myharness.providers.base import (  # noqa: E402
    ProviderError,
    extract_error_detail,
    hint_for_status,
)
from myharness.providers.openai import OpenAIProvider  # noqa: E402


# ============================================================
# 测试替身：不碰网络，只负责按剧本抛异常 / 返回响应
# ============================================================

def _request():
    return httpx2.Request("POST", "https://example.invalid/v1/chat/completions")


def _status_error(exc_cls, status: int, *, body=None, headers=None):
    """构造一个真实的 SDK 状态码异常（带真实 HTTP 响应对象）。"""
    payload = body if body is not None else {
        "error": {"message": f"boom {status}", "type": "TestError", "code": f"Test.{status}"}
    }
    response = httpx2.Response(
        status, request=_request(), json=payload, headers=headers or {}
    )
    return exc_cls(f"Error code: {status} - {payload}", response=response, body=payload)


def _completion(text: str):
    """最小可用的 OpenAI ChatCompletion 替身。"""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text, tool_calls=None))],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=22),
    )


class FakeClient:
    """按剧本逐次响应的 SDK client 替身。

    同时暴露 openai 的 `chat.completions.create` 与 anthropic 的
    `messages.create`，因为两条路径在测试里的用法完全一样。
    """

    def __init__(self, outcomes: list):
        self._outcomes = outcomes
        self.calls = 0
        self.last_kwargs: dict | None = None
        self.base_url = "https://example.invalid/v1"
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.last_kwargs = kwargs
        # 剧本用尽后重复最后一项，便于「一直失败」的场景
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _provider(client: FakeClient, cls=OpenAIProvider, *, max_retries: int = 2):
    """构造一个 client 被替换掉的 provider（不产生真实调用）。"""
    provider = cls(model="test-model", api_key="sk-test")
    provider.client = client
    provider.max_retries = max_retries
    provider.retry_base_delay = 0
    return provider


def _chat(provider, text: str = "hi"):
    return provider.chat(
        messages=[{"role": "user", "content": text}],
        tools=[],
        system="system",
        max_tokens=16,
    )


# ============================================================
# 错误体的解析
# ============================================================

def test_extract_error_detail_handles_wrapped_body():
    """{"error": {...}} —— 原始响应体形状。"""
    code, detail = extract_error_detail(
        {"error": {"code": "AccessDenied.Unpurchased", "message": "denied"}}
    )
    assert code == "AccessDenied.Unpurchased"
    assert detail == "denied"


def test_extract_error_detail_handles_flat_body():
    """{...} —— SDK 已取出的 error 节点形状。"""
    code, detail = extract_error_detail({"type": "rate_limited", "message": "slow down"})
    assert code == "rate_limited"
    assert detail == "slow down"


def test_extract_error_detail_tolerates_missing_body():
    assert extract_error_detail(None) == (None, "")


@pytest.mark.parametrize(
    "status, needle",
    [
        (401, "密钥"),
        (403, "MODEL_ID"),
        (404, "MODEL_ID"),
        (429, "限流"),
        (500, "服务端"),
        (503, "服务端"),
    ],
)
def test_hints_are_actionable(status, needle):
    """修复建议必须具体到「怎么改」，而不是复述状态码。"""
    hint = hint_for_status(status)
    assert hint is not None
    assert needle in hint


def test_unmapped_status_has_no_hint():
    assert hint_for_status(418) is None


# ============================================================
# 工具定义格式：Anthropic -> OpenAI function calling
# ============================================================

def test_tools_are_converted_to_openai_function_shape():
    """回归：漏掉工具转换会让端点直接 400（'type' is a required property）。"""
    client = FakeClient([_completion("ok")])
    provider = _provider(client)

    provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[{
            "name": "bash",
            "description": "Execute a bash command.",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }],
        system="s",
        max_tokens=16,
    )

    sent = client.last_kwargs["tools"]
    assert len(sent) == 1
    assert sent[0]["type"] == "function"
    assert set(sent[0]) == {"type", "function"}
    assert sent[0]["function"]["name"] == "bash"
    assert sent[0]["function"]["description"] == "Execute a bash command."
    assert sent[0]["function"]["parameters"]["required"] == ["command"]
    # Anthropic 的 key 不能泄漏到 OpenAI 请求体里
    assert "input_schema" not in sent[0]


def test_whole_real_tool_pool_converts_to_valid_definitions():
    """整池真实工具都要能转成合法定义，而不只是测试里的手写样本。"""
    from myharness.config import skill_library
    from myharness.tools import TOOLS

    tools = list(TOOLS) + skill_library.tool_defs()
    converted = OpenAIProvider._convert_tools_to_openai(tools)

    assert [t["function"]["name"] for t in converted] == [t["name"] for t in tools]
    assert "bash" in {t["function"]["name"] for t in converted}
    assert "load_skill" in {t["function"]["name"] for t in converted}
    for item in converted:
        assert item["type"] == "function"
        assert item["function"]["parameters"]["type"] == "object"


def test_tool_without_schema_still_gets_a_valid_object_schema():
    """无参数工具（如 list_tasks）也必须给出合法 parameters。"""
    converted = OpenAIProvider._convert_tools_to_openai(
        [{"name": "list_tasks", "description": "list"}]
    )
    assert converted[0]["function"]["parameters"] == {"type": "object", "properties": {}}


def test_empty_tools_are_sent_as_none():
    """没有工具时应传 None，而不是空列表 —— 空列表会被部分端点视为非法。"""
    client = FakeClient([_completion("ok")])
    _chat(_provider(client))
    assert client.last_kwargs["tools"] is None


# ============================================================
# 工具调用参数：截断与坏 JSON 必须归一成 ProviderError
# ============================================================

def _tool_completion(name: str, arguments: str, *, finish_reason="tool_calls"):
    """带一个 tool_call 的 OpenAI 响应替身。"""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=None,
                    tool_calls=[
                        SimpleNamespace(
                            id="call_1",
                            type="function",
                            function=SimpleNamespace(name=name, arguments=arguments),
                        )
                    ],
                ),
                finish_reason=finish_reason,
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def test_tool_call_arguments_are_parsed():
    """正常路径：arguments 里的 JSON 被解析成 dict。"""
    client = FakeClient([_tool_completion("write_file", '{"path": "a.py", "content": "x"}')])
    response = _chat(_provider(client))

    block = response.content[0]
    assert block.type == "tool_use"
    assert block.input == {"path": "a.py", "content": "x"}


def test_empty_tool_arguments_become_empty_dict():
    """部分端点在无参数工具上给空字符串，不能当成 JSON 错误。"""
    client = FakeClient([_tool_completion("list_tasks", "")])
    response = _chat(_provider(client))
    assert response.content[0].input == {}


def test_truncated_tool_arguments_raise_provider_error():
    """MAX_TOKENS 截断：JSONDecodeError 不得穿出 provider 层，且不该重试。

    截断是确定性的 —— 同样的 max_tokens 重发只会得到同样的半截 JSON。
    """
    client = FakeClient([
        _tool_completion("write_file", '{"path": "a.py", "content": "def f(', finish_reason="length")
    ])
    provider = _provider(client)

    with pytest.raises(ProviderError) as excinfo:
        _chat(provider)

    error = excinfo.value
    assert client.calls == 1, "截断不该重试"
    assert error.retryable is False
    assert error.code == "invalid_tool_arguments"
    assert "截断" in error.message
    assert "MAX_TOKENS" in error.hint


def test_malformed_arguments_are_retried_then_succeed():
    """没有截断标记的坏 JSON 通常是随机翻车，重试可能成功。"""
    client = FakeClient([
        _tool_completion("write_file", '{"path": "a.py"'),
        _tool_completion("write_file", '{"path": "a.py"}'),
    ])
    response = _chat(_provider(client))

    assert client.calls == 2
    assert response.content[0].input == {"path": "a.py"}


def test_malformed_arguments_exhaust_retries_with_attempt_count():
    """一直坏下去就带着尝试次数报错，而不是无限重试。"""
    bad = _tool_completion("write_file", "{oops")
    client = FakeClient([bad, bad, bad])

    with pytest.raises(ProviderError) as excinfo:
        _chat(_provider(client, max_retries=2))

    assert client.calls == 3
    assert excinfo.value.attempts == 3
    assert excinfo.value.retryable is True


def test_non_object_arguments_are_rejected():
    """合法 JSON 但不是对象（例如字符串）同样不能进 dispatch。"""
    client = FakeClient([_tool_completion("write_file", '"just a string"')])

    with pytest.raises(ProviderError, match="不是 JSON 对象"):
        _chat(_provider(client))


# ============================================================
# 流式输出
# ============================================================
#
# 不变量：传不传 on_text，返回的响应对象必须一模一样 —— 流式只是传输细节。

def _chunk(content=None, *, tool_calls=None, finish_reason=None, usage=None):
    """一个 OpenAI 流式 chunk 的替身（choices 可能为空，如只带 usage 的尾块）。"""
    choices = []
    if content is not None or tool_calls is not None or finish_reason is not None:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(choices=choices, usage=usage)


def _tc(index, *, id=None, name=None, arguments=None):
    """tool_call 的增量片段：id/name 通常只在第一片出现。"""
    return SimpleNamespace(
        index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def _failing_stream(pieces, exc):
    """先产出若干 chunk，再抛异常 —— 模拟「输出到一半连接断了」。"""

    def generate():
        for piece in pieces:
            yield piece
        raise exc

    return generate()


def test_streaming_forwards_text_and_returns_the_same_shape():
    client = FakeClient([[
        _chunk("你"),
        _chunk("好"),
        _chunk(finish_reason="stop"),
        _chunk(usage=SimpleNamespace(prompt_tokens=7, completion_tokens=2)),
    ]])
    seen: list[str] = []

    response = _provider(client).chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        system="s",
        max_tokens=16,
        on_text=seen.append,
    )

    assert seen == ["你", "好"]
    assert response.content[0].type == "text"
    assert response.content[0].text == "你好"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (7, 2)
    assert client.last_kwargs["stream"] is True


def test_non_streaming_request_has_no_stream_flag():
    """不传回调时保持原来的非流式请求，不要偷偷打开 stream。"""
    client = FakeClient([_completion("ok")])
    _chat(_provider(client))
    assert "stream" not in client.last_kwargs


def test_streaming_assembles_tool_arguments_across_chunks():
    """参数按 index 分片到达，拼完才是合法 JSON；文本分片为空时不该打前缀。"""
    client = FakeClient([[
        _chunk(tool_calls=[_tc(0, id="call_1", name="write_file", arguments='{"path": ')]),
        _chunk(tool_calls=[_tc(0, arguments='"a.py", "content": "x"}')]),
        _chunk(finish_reason="tool_calls"),
    ]])
    seen: list[str] = []

    response = _provider(client).chat(
        messages=[{"role": "user", "content": "写文件"}],
        tools=[],
        system="s",
        max_tokens=16,
        on_text=seen.append,
    )

    assert response.content[0].type == "tool_use"
    assert response.content[0].input == {"path": "a.py", "content": "x"}
    assert seen == []


def test_streaming_detects_truncated_tool_arguments():
    """流式同样要认出「截断」：JSON 拼不完整且 finish_reason=length。"""
    client = FakeClient([[
        _chunk(tool_calls=[_tc(0, id="call_1", name="write_file", arguments='{"path": "a.py"')]),
        _chunk(finish_reason="length"),
    ]])

    with pytest.raises(ProviderError, match="截断") as excinfo:
        _provider(client).chat(
            messages=[{"role": "user", "content": "写大文件"}],
            tools=[],
            system="s",
            max_tokens=16,
            on_text=lambda _: None,
        )

    assert client.calls == 1, "截断不重试"
    assert excinfo.value.retryable is False


def test_stream_failure_after_output_is_not_retried():
    """已经打出去的字收不回来 —— 中断只能报错，不能重试。"""
    client = FakeClient([
        _failing_stream([_chunk("半个")], openai.APIConnectionError(request=_request()))
    ])
    seen: list[str] = []

    with pytest.raises(ProviderError) as excinfo:
        _provider(client).chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system="s",
            max_tokens=16,
            on_text=seen.append,
        )

    assert client.calls == 1
    assert seen == ["半个"]
    assert excinfo.value.retryable is False
    assert "流式输出中断" in excinfo.value.message


def test_stream_retries_when_nothing_was_printed():
    """一个字符都没输出时，连接失败仍按可重试处理。"""
    client = FakeClient([
        openai.APIConnectionError(request=_request()),
        [_chunk("ok")],
    ])
    seen: list[str] = []

    response = _provider(client).chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        system="s",
        max_tokens=16,
        on_text=seen.append,
    )

    assert client.calls == 2
    assert seen == ["ok"]
    assert response.content[0].text == "ok"


def test_stream_falls_back_when_endpoint_rejects_stream_options(capsys):
    """部分兼容端点直接 400 拒绝 stream_options：退回普通流，别让整轮失败。"""
    client = FakeClient([
        _status_error(openai.BadRequestError, 400),
        [_chunk("hi")],
    ])
    seen: list[str] = []

    response = _provider(client).chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        system="s",
        max_tokens=16,
        on_text=seen.append,
    )

    assert client.calls == 2
    assert seen == ["hi"]
    assert "stream_options" not in client.last_kwargs
    assert "usage" in capsys.readouterr().out
    assert response.usage.output_tokens >= 1, "usage 缺失时应有估算，而不是 0"


class FakeMessageStream:
    """anthropic `messages.stream()` 的替身。"""

    def __init__(self, pieces, final, *, fail_with=None):
        self.pieces = pieces
        self.final = final
        self.fail_with = fail_with

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    @property
    def text_stream(self):
        for piece in self.pieces:
            yield piece
        if self.fail_with is not None:
            raise self.fail_with

    def get_final_message(self):
        return self.final


class FakeStreamingAnthropicClient:
    base_url = "https://example.invalid"

    def __init__(self, stream):
        self.stream = stream
        self.calls = 0
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.calls += 1
        return self.stream


def _anthropic_message(text="你好"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=3, output_tokens=2),
    )


def test_anthropic_streams_text_and_returns_final_message():
    final = _anthropic_message()
    client = FakeStreamingAnthropicClient(FakeMessageStream(["你", "好"], final))
    seen: list[str] = []

    response = _provider(client, AnthropicProvider).chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[],
        system="s",
        max_tokens=16,
        on_text=seen.append,
    )

    assert seen == ["你", "好"]
    assert response is final


def test_anthropic_stream_failure_after_output_is_not_retried():
    stream = FakeMessageStream(
        ["半"], None, fail_with=anthropic.APIConnectionError(request=_request())
    )
    client = FakeStreamingAnthropicClient(stream)

    with pytest.raises(ProviderError) as excinfo:
        _provider(client, AnthropicProvider).chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system="s",
            max_tokens=16,
            on_text=lambda _: None,
        )

    assert client.calls == 1
    assert excinfo.value.retryable is False
    assert "流式输出中断" in excinfo.value.message


def test_agent_loop_streams_text_to_the_callback(monkeypatch):
    """端到端：loop 把 on_text 透传给 provider，历史里仍是完整消息。"""
    client = FakeClient([[_chunk("你"), _chunk("好")]])
    monkeypatch.setattr(loop, "provider", _provider(client, max_retries=0))

    seen: list[str] = []
    messages = [{"role": "user", "content": "hi"}]
    loop.agent_loop(messages, on_text=seen.append)

    assert seen == ["你", "好"]
    assert messages[-1]["content"][0]["text"] == "你好"


# ============================================================
# 403：真实故障场景
# ============================================================

def test_403_is_not_retried_and_carries_the_vendor_code():
    """权限拒绝属于永久性错误：只请求一次，并且把服务端错误码带出来。"""
    client = FakeClient([_status_error(openai.PermissionDeniedError, 403)])
    provider = _provider(client)

    with pytest.raises(ProviderError) as excinfo:
        _chat(provider)

    error = excinfo.value
    assert client.calls == 1, "403 不该重试"
    assert error.retryable is False
    assert error.status_code == 403
    assert error.code == "Test.403"
    assert error.attempts == 1
    assert "MODEL_ID" in error.hint


def test_dashscope_403_payload_is_parsed():
    """DashScope 的真实响应体：错误码在 error.code 里。"""
    payload = {
        "error": {
            "message": "Access to model denied. Please make sure you are eligible for using the model.",
            "id": "5ff436ae-c35d-9fb5-82fa-d73451c64284",
            "type": "AccessDenied.Unpurchased",
            "code": "AccessDenied.Unpurchased",
        }
    }
    client = FakeClient([_status_error(openai.PermissionDeniedError, 403, body=payload)])
    provider = _provider(client)

    with pytest.raises(ProviderError) as excinfo:
        _chat(provider)

    error = excinfo.value
    assert error.code == "AccessDenied.Unpurchased"
    assert "Access to model denied" in error.message
    assert error.message.startswith("HTTP 403")
    # 原始异常链必须保留，日志里还能看到 SDK 的完整信息
    assert isinstance(excinfo.value.__cause__, openai.PermissionDeniedError)


def test_anthropic_provider_normalizes_the_same_way():
    """两个 provider 的错误语义必须一致，否则切换模型会换一套故障表现。"""
    client = FakeClient([_status_error(anthropic.PermissionDeniedError, 403)])
    provider = _provider(client, cls=AnthropicProvider)

    with pytest.raises(ProviderError) as excinfo:
        _chat(provider)

    assert client.calls == 1
    assert excinfo.value.status_code == 403
    assert excinfo.value.retryable is False


# ============================================================
# 重试：只重试可重试的，且有上界
# ============================================================

def test_timeout_is_retried_then_succeeds(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(base.time, "sleep", slept.append)

    client = FakeClient([
        openai.APITimeoutError(_request()),
        openai.APITimeoutError(_request()),
        _completion("recovered"),
    ])
    provider = _provider(client)

    response = _chat(provider)

    assert client.calls == 3
    assert len(slept) == 2, "两次重试各退避一次"
    assert response.content[0].text == "recovered"
    assert response.usage.input_tokens == 11


def test_5xx_is_retried_but_bounded(monkeypatch):
    """重试次数有上界：一直失败就停手，不能无限重试。"""
    monkeypatch.setattr(base.time, "sleep", lambda _: None)

    client = FakeClient([_status_error(openai.InternalServerError, 503)])
    provider = _provider(client, max_retries=2)

    with pytest.raises(ProviderError) as excinfo:
        _chat(provider)

    assert client.calls == 3, "1 次首发 + 2 次重试"
    assert excinfo.value.attempts == 3
    assert excinfo.value.retryable is True


def test_retry_after_header_wins_over_backoff(monkeypatch):
    """服务端给了 retry-after 就听它的，别自己硬算退避。"""
    slept: list[float] = []
    monkeypatch.setattr(base.time, "sleep", slept.append)

    client = FakeClient([
        _status_error(openai.RateLimitError, 429, headers={"retry-after": "1.5"}),
        _completion("ok"),
    ])
    _chat(_provider(client))

    assert slept == [1.5]


def test_retry_is_announced(monkeypatch, capsys):
    """重试要打出来，否则用户会以为程序卡住。"""
    monkeypatch.setattr(base.time, "sleep", lambda _: None)

    client = FakeClient([openai.APITimeoutError(_request()), _completion("ok")])
    _chat(_provider(client))

    assert "重试" in capsys.readouterr().out


def test_retry_after_is_capped(monkeypatch):
    """服务端给出夸张的 retry-after 时不能把 CLI 挂住。"""
    slept: list[float] = []
    monkeypatch.setattr(base.time, "sleep", slept.append)

    client = FakeClient([
        _status_error(openai.RateLimitError, 429, headers={"retry-after": "600"}),
        _completion("ok"),
    ])
    _chat(_provider(client))

    assert slept == [base.MAX_RETRY_DELAY]


# ============================================================
# agent_loop：失败必须优雅收尾
# ============================================================

@pytest.fixture(autouse=True)
def clean_hooks():
    """loop 会触发 Stop hook；清空注册表以免误发真实请求。"""
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


class _FailingProvider:
    def chat(self, **kwargs):
        raise ProviderError(
            "HTTP 403 AccessDenied.Unpurchased: Access to model denied.",
            status_code=403,
            code="AccessDenied.Unpurchased",
            hint="账号无权调用该模型（403）。",
        )


def test_agent_loop_survives_provider_error(monkeypatch, capsys):
    """回归：模型调用失败不得抛穿 agent_loop。"""
    messages = [{"role": "user", "content": "帮我做一个音乐播放器，UI界面要好看"}]

    monkeypatch.setattr(loop, "provider", _FailingProvider())
    loop.agent_loop(messages)  # 不抛异常

    assert len(messages) == 1, "失败的轮次不得写入半截 assistant 消息，下一轮要能重问"
    out = capsys.readouterr().out
    assert "模型调用失败" in out
    assert "Access to model denied" in out
    assert "账号无权调用该模型" in out


def test_agent_loop_survives_truncated_tool_arguments(monkeypatch, capsys):
    """报告里的原场景：MAX_TOKENS 截断 → ProviderError → 本轮优雅结束。

    修复前 JSONDecodeError 会穿出 provider，再穿出 agent_loop，CLI 带着
    traceback 崩掉，尚未保存的会话一起丢失。
    """
    client = FakeClient([
        _tool_completion(
            "write_file",
            '{"path": "a.py", "content": "def f(\\n    return 1\\n',
            finish_reason="length",
        )
    ])
    monkeypatch.setattr(loop, "provider", _provider(client, max_retries=0))

    messages = [{"role": "user", "content": "写一个大文件"}]
    loop.agent_loop(messages)  # 不抛异常

    assert len(messages) == 1, "失败的轮次不得留下半截 assistant 消息"
    out = capsys.readouterr().out
    assert "截断" in out
    assert "MAX_TOKENS" in out


def test_agent_loop_reports_retry_count(monkeypatch, capsys):
    """重试过就要说清楚，避免「为什么这么慢」无从判断。"""

    class SlowFailProvider:
        def chat(self, **kwargs):
            raise ProviderError("请求超时", retryable=True, attempts=3)

    monkeypatch.setattr(loop, "provider", SlowFailProvider())
    loop.agent_loop([{"role": "user", "content": "hi"}])

    assert "已尝试 3 次" in capsys.readouterr().out


def test_agent_loop_does_not_swallow_other_exceptions(monkeypatch):
    """只捕获 ProviderError：其它异常是代码缺陷，必须暴露而不是被吞掉。"""

    class BuggyProvider:
        def chat(self, **kwargs):
            raise ValueError("adapter bug")

    monkeypatch.setattr(loop, "provider", BuggyProvider())
    with pytest.raises(ValueError, match="adapter bug"):
        loop.agent_loop([{"role": "user", "content": "hi"}])
