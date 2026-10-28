"""上下文预算：按 token 压缩，而不是按消息条数。

背景：旧实现只看「消息是否超过 30 条」，于是一条 4 万字符的 tool_result
（约 1 万 token）能安然躺在历史里，直到 loop 的累计预算把整轮砍掉 ——
用户看到的却是「才两轮对话就结束」。这里锁住按 token 的判据。
"""

import json

import pytest

from myharness import loop
from myharness.context import (
    ContextManager,
    estimate_message_tokens,
    estimate_tokens,
)


# ============================================================
# 估算器
# ============================================================

def test_estimate_counts_cjk_heavier_than_ascii():
    """中文 1 字符 ≈ 1 token，英文 3.5 字符 ≈ 1 token —— 不能一视同仁。"""
    chinese = estimate_tokens("上" * 350)
    english = estimate_tokens("a" * 350)

    assert 340 <= chinese <= 360
    assert 90 <= english <= 110
    assert chinese > english * 3


def test_estimate_handles_empty():
    assert estimate_tokens("") == 0


def test_estimate_message_tokens_counts_structure_too():
    """结构字段（role/type/id/input 的键名）也要计入，否则低估工具历史。"""
    message = {
        "role": "assistant",
        "content": [
            {
                "type": "tool_use",
                "id": "call_abcdef",
                "name": "read_file",
                "input": {"path": "a.py"},
            }
        ],
    }
    assert estimate_message_tokens(message) == estimate_tokens(
        json.dumps(message, ensure_ascii=False)
    )


# ============================================================
# 触发条件
# ============================================================

def test_compress_triggers_on_tokens_not_message_count():
    """只有 3 条消息，但一条 tool_result 就吃满预算 —— 必须压缩。"""
    messages = [
        {"role": "user", "content": "读一下这个大文件"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 200_000}
            ],
        },
    ]
    manager = ContextManager(max_messages=30, max_tokens=20_000)

    assert len(messages) == 3, "条数远未超限，旧实现会放行"
    assert manager.should_compress(messages) is True


def test_short_history_is_left_alone():
    """正常对话不该被动：压缩会丢信息，能不压就不压。"""
    messages = [{"role": "user", "content": "你好"}]
    manager = ContextManager(max_messages=30, max_tokens=100_000)

    assert manager.should_compress(messages) is False
    assert manager.compress(messages) is messages


# ============================================================
# 压缩行为
# ============================================================

def _round(index: int, payload: str = "ok") -> list[dict]:
    """一轮完整对话：用户请求 -> 工具调用 -> 结果。"""
    return [
        {"role": "user", "content": f"第 {index} 个请求"},
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": f"t{index}", "name": "read_file", "input": {}}
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": f"t{index}", "content": payload}
            ],
        },
    ]


def test_compress_drops_oldest_rounds_and_keeps_first_message_valid():
    """切点必须落在用户请求上：历史首条若是 tool_result，Anthropic 直接 400。"""
    messages = _round(1) + _round(2) + _round(3)
    manager = ContextManager(max_messages=30, max_tokens=200, target_ratio=0.4)

    compressed = manager.compress(messages)

    assert len(compressed) < len(messages)
    assert compressed[0]["role"] == "user"
    assert isinstance(compressed[0]["content"], str), "首条不能是 tool_result"
    assert "第 3 个请求" in compressed[0]["content"], "当前这一轮不能被切掉"


def test_compress_never_orphans_tool_results():
    """任何切法下，每个 tool_result 都要有对应的 tool_use。"""
    messages = _round(1) + _round(2) + _round(3)
    manager = ContextManager(max_messages=4, max_tokens=None)

    compressed = manager.compress(messages)

    uses = {
        block["id"]
        for message in compressed
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    }
    results = {
        block["tool_use_id"]
        for message in compressed
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    assert results <= uses, "出现了没有 tool_use 的 tool_result"
    assert compressed[0]["role"] == "user"
    assert isinstance(compressed[0]["content"], str)


def test_giant_tool_result_is_truncated_with_marker():
    """一条结果自己就超预算时，只能截断它，而不是把整轮扔掉。"""
    messages = [
        {"role": "user", "content": "读一下"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "x" * 200_000}
            ],
        },
    ]
    manager = ContextManager(max_messages=30, max_tokens=20_000)

    compressed = manager.compress(messages)
    result = compressed[-1]["content"][0]["content"]

    assert len(result) < 200_000
    assert "已截断" in result
    assert result.startswith("x" * 100), "保留开头，模型仍能看到读取结果的前半段"


def test_truncation_does_not_mutate_caller_history():
    """历史同时躺在 session 里（会被存盘），压缩不能就地改坏它。"""
    original = "x" * 200_000
    messages = [
        {"role": "user", "content": "读一下"},
        {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": "read_file", "input": {}}],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": original}],
        },
    ]
    manager = ContextManager(max_messages=30, max_tokens=20_000)

    manager.compress(messages)

    assert messages[-1]["content"][0]["content"] is original
    assert len(messages[-1]["content"][0]["content"]) == 200_000


# ============================================================
# loop 层：两条上限的说法不能混
# ============================================================

class _ScriptedProvider:
    def __init__(self, responses):
        self._responses = list(responses)

    def chat(self, messages, tools, system, max_tokens, on_text=None):
        return self._responses.pop(0)


def _text_response(text: str, input_tokens: int = 10, output_tokens: int = 10):
    class Block:
        type = "text"

        def __init__(self, value):
            self.text = value

        def model_dump(self):
            return {"type": "text", "text": self.text}

    class Usage:
        pass

    usage = Usage()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens

    class Response:
        pass

    response = Response()
    response.content = [Block(text)]
    response.usage = usage
    return response


def test_budget_message_distinguishes_cumulative_from_context(monkeypatch, capsys):
    """超预算时要说清楚：超的是累计消耗，不是上下文长度。"""
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10)
    monkeypatch.setattr(loop, "provider", _ScriptedProvider([_text_response("好")]))

    loop.agent_loop([{"role": "user", "content": "hi"}])

    out = capsys.readouterr().out
    assert "token 预算上限" in out
    assert "本轮累计" in out
    assert "当前上下文约" in out


def test_loop_compresses_before_request(monkeypatch, capsys):
    """压缩必须发生在请求之前，并且把压缩前后的 token 都报出来。"""
    manager = ContextManager(max_messages=30, max_tokens=200, target_ratio=0.4)
    monkeypatch.setattr(loop, "context_manager", manager)
    monkeypatch.setattr(loop, "provider", _ScriptedProvider([_text_response("好")]))

    messages = [
        {"role": "user", "content": "旧请求"},
        {"role": "assistant", "content": [{"type": "text", "text": "旧回答" * 200}]},
        {"role": "user", "content": "新请求"},
    ]

    loop.agent_loop(messages)

    out = capsys.readouterr().out
    assert "[context]" in out
    assert "tokens" in out
    assert messages[0]["content"] == "新请求", "压缩后当前请求必须还在"
