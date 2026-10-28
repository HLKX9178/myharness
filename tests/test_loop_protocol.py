"""Agent Loop 的消息协议不变量。

回归目标：token 预算耗尽时 ``agent_loop`` 直接 ``return``，而这一轮的
assistant 消息已经写进 ``messages`` —— 里面可能带着 ``tool_use``。于是历史里
留下一组没有 ``tool_result`` 的悬空调用，被 session 持久化后下一轮发给模型：

  - Anthropic 兼容端点通常要求 tool_use 后必须跟 tool_result，直接 400；
  - 即使端点宽容，模型看到的也是「问了一半」的对话，会反复重试同一个调用。

本文件的核心断言只有一条：**无论循环从哪个出口结束，messages 里每个
tool_use 都能找到同 id 的 tool_result。**
"""

from types import SimpleNamespace

import pytest

from myharness import hooks, loop


# ============================================================
# 工具函数
# ============================================================

def _unpaired_tool_use(messages: list) -> set[str]:
    """返回历史中没有 tool_result 的 tool_use id。"""
    pending: set[str] = set()
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                pending.add(block.get("id"))
            elif block.get("type") == "tool_result":
                pending.discard(block.get("tool_use_id"))
    return pending


def _text(text: str):
    return SimpleNamespace(type="text", text=text)


def _tool_use(tool_id: str, name: str = "read_file", args: dict | None = None):
    return SimpleNamespace(type="tool_use", id=tool_id, name=name, input=args or {})


def _response(content: list, input_tokens: int = 1000, output_tokens: int = 1000):
    return SimpleNamespace(
        content=content,
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ),
    )


class _ScriptedProvider:
    """按剧本返回 response；剧本用尽后重复最后一条。"""

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0

    def chat(self, **kwargs):
        response = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        return response


@pytest.fixture(autouse=True)
def clean_hooks():
    """loop 会触发 Stop hook；清空注册表以免误发真实请求。"""
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


# ============================================================
# token 预算耗尽
# ============================================================

def test_token_exhausted_pairs_pending_tool_use(monkeypatch, capsys):
    """回归：超预算退出前必须给 tool_use 补上 tool_result。"""
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10)
    monkeypatch.setattr(
        loop,
        "provider",
        _ScriptedProvider([_response([_tool_use("call_1")], 100, 100)]),
    )

    messages = [{"role": "user", "content": "hi"}]
    loop.agent_loop(messages)

    assert _unpaired_tool_use(messages) == set(), "tool_use 没有配对的 tool_result"
    assert len(messages) == 3
    assert messages[1]["role"] == "assistant"

    result_blocks = messages[2]["content"]
    assert messages[2]["role"] == "user"
    assert len(result_blocks) == 1
    assert result_blocks[0]["type"] == "tool_result"
    assert result_blocks[0]["tool_use_id"] == "call_1"
    assert result_blocks[0]["is_error"] is True
    assert "token" in result_blocks[0]["content"]

    assert "token 预算上限" in capsys.readouterr().out


def test_token_exhausted_pairs_every_tool_use_in_batch(monkeypatch):
    """一轮里有多个 tool_use 时，必须逐个补齐，不能只补第一个。"""
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10)
    monkeypatch.setattr(
        loop,
        "provider",
        _ScriptedProvider([_response([
            _text("我需要同时看两个文件"),
            _tool_use("call_a"),
            _tool_use("call_b"),
            _tool_use("call_c"),
        ], 100, 100)]),
    )

    messages = [{"role": "user", "content": "hi"}]
    loop.agent_loop(messages)

    assert _unpaired_tool_use(messages) == set()
    answered = [
        block["tool_use_id"]
        for block in messages[-1]["content"]
        if block.get("type") == "tool_result"
    ]
    assert answered == ["call_a", "call_b", "call_c"]


def test_token_exhausted_does_not_execute_tool(monkeypatch):
    """预算已耗尽，工具不该真的执行：副作用无法回收，且上下文只会更大。"""
    executed: list[str] = []
    hooks.register_hook("PreToolUse", lambda block: executed.append(block.name))

    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10)
    monkeypatch.setattr(
        loop,
        "provider",
        _ScriptedProvider([_response([_tool_use("call_1", "write_file")], 100, 100)]),
    )

    loop.agent_loop([{"role": "user", "content": "hi"}])

    assert executed == [], "超预算的工具调用不得进入 execute_tool"


def test_token_exhausted_without_tool_use_adds_nothing(monkeypatch):
    """模型已经给出最终答复时，超预算退出不该凭空多出一条消息。"""
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10)
    monkeypatch.setattr(
        loop,
        "provider",
        _ScriptedProvider([_response([_text("这是最终答案")], 100, 100)]),
    )

    messages = [{"role": "user", "content": "hi"}]
    loop.agent_loop(messages)

    assert len(messages) == 2
    assert messages[1]["role"] == "assistant"


def test_tool_use_without_id_is_skipped_not_crashed(monkeypatch, capsys):
    """provider 漏给 id 时不能崩：配不上的块只能跳过并留下痕迹。"""
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10)
    monkeypatch.setattr(
        loop,
        "provider",
        _ScriptedProvider([_response([_tool_use("", "read_file")], 100, 100)]),
    )

    messages = [{"role": "user", "content": "hi"}]
    loop.agent_loop(messages)  # 不抛异常

    assert "无法补配 tool_result" in capsys.readouterr().out


# ============================================================
# 基线：正常路径本来就是配对的
# ============================================================

def test_normal_tool_round_trip_stays_paired(monkeypatch):
    """未超限时，工具正常执行并回填，配对由 execute_tool 保证。"""
    monkeypatch.setattr(loop, "TURN_TOKEN_BUDGET", 10_000_000)
    monkeypatch.setattr(
        loop,
        "provider",
        _ScriptedProvider([
            _response([_tool_use("call_1", "list_tasks")], 10, 10),
            _response([_text("做完了")], 10, 10),
        ]),
    )

    messages = [{"role": "user", "content": "hi"}]
    loop.agent_loop(messages)

    assert _unpaired_tool_use(messages) == set()
    assert messages[-1]["role"] == "assistant"


# ============================================================
# 入口防御：修复旧会话里的悬空 tool_use
# ============================================================

def _assistant_with_tool_use(*tool_ids: str) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_id, "name": "read_file", "input": {}}
            for tool_id in tool_ids
        ],
    }


def test_repair_appends_missing_result_at_tail():
    """旧会话最后一条是 assistant(tool_use) —— 补齐后可直接续跑。"""
    messages = [
        {"role": "user", "content": "看看这个文件"},
        _assistant_with_tool_use("call_1"),
    ]

    assert loop._repair_orphan_tool_uses(messages) == 1
    assert len(messages) == 3
    assert messages[2]["role"] == "user"
    assert messages[2]["content"][0]["tool_use_id"] == "call_1"
    assert _unpaired_tool_use(messages) == set()


def test_repair_inserts_into_middle_when_conversation_continued():
    """悬空调用后面还有新消息时，必须插在它后面而不是补到末尾。"""
    messages = [
        {"role": "user", "content": "看看这个文件"},
        _assistant_with_tool_use("call_1"),
        {"role": "user", "content": "算了，换个问题"},
        {"role": "assistant", "content": [{"type": "text", "text": "好的"}]},
    ]

    assert loop._repair_orphan_tool_uses(messages) == 1
    assert messages[2]["role"] == "user"
    assert messages[2]["content"][0]["tool_use_id"] == "call_1"
    assert messages[3]["content"] == "算了，换个问题"
    assert _unpaired_tool_use(messages) == set()


def test_repair_only_fills_the_missing_half_of_a_batch():
    """一轮两个 tool_use 只回了一个结果：只补缺的那个，不动已有的。"""
    messages = [
        {"role": "user", "content": "看两个文件"},
        _assistant_with_tool_use("call_a", "call_b"),
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_a", "content": "内容 A"},
            ],
        },
    ]

    assert loop._repair_orphan_tool_uses(messages) == 1
    assert len(messages) == 3, "应合并进已有的 tool_result 消息，而不是再加一条"
    ids = [block["tool_use_id"] for block in messages[2]["content"]]
    assert ids == ["call_a", "call_b"]
    assert messages[2]["content"][0]["content"] == "内容 A", "已有结果不得被改写"


def test_repair_is_idempotent():
    """健康历史不产生任何改动，反复调用也不会无限追加。"""
    messages = [
        {"role": "user", "content": "hi"},
        _assistant_with_tool_use("call_1"),
        {
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "ok"},
            ],
        },
        {"role": "assistant", "content": [{"type": "text", "text": "完成"}]},
    ]

    assert loop._repair_orphan_tool_uses(messages) == 0
    assert len(messages) == 4
    assert loop._repair_orphan_tool_uses(messages) == 0
    assert len(messages) == 4


def test_agent_loop_repairs_before_first_request(monkeypatch, capsys):
    """端到端：脏历史必须在发给模型前就被修好。"""
    seen: list[list] = []

    class RecordingProvider:
        def chat(self, messages, **kwargs):
            seen.append([dict(m) for m in messages])
            return _response([_text("好")], 1, 1)

    monkeypatch.setattr(loop, "provider", RecordingProvider())
    messages = [
        {"role": "user", "content": "旧问题"},
        _assistant_with_tool_use("call_old"),
    ]

    loop.agent_loop(messages)

    assert _unpaired_tool_use(seen[0]) == set(), "发给模型的 history 仍不合法"
    assert "缺少结果" in capsys.readouterr().out
