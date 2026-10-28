"""前缀稳定性：system prompt 必须跨请求逐字节相同。

请求前缀是 `[tools][system][messages...]`。system 在 messages 之前，所以
system 里只要有一个字节随用户输入变化，后面整段历史都无法命中前缀缓存。
长期记忆曾经拼在 system 末尾，命中一次记忆就作废一次历史；现在记忆走
`UserPromptSubmit` 注入用户消息 —— 那段本来就是每轮新增的。

这里守住三件事：

1. 同一次 `agent_loop` 里每次模型调用拿到的 system 完全一致；
2. `build_system_prompt()` 不读记忆库、也不接收用户输入；
3. usage 里的缓存命中字段能被读出来（否则命中率永远看不见）。
"""

from types import SimpleNamespace

from myharness import hooks, loop
from myharness.bootstrap import install_default_hooks
from myharness.memory import MemoryStore
from myharness.providers.base import cached_prompt_tokens


class _Block:
    def __init__(self, type_, **fields):
        self.type = type_
        for key, value in fields.items():
            setattr(self, key, value)

    def model_dump(self):
        data = {"type": self.type}
        for key in ("text", "id", "name", "input"):
            if hasattr(self, key):
                data[key] = getattr(self, key)
        return data


class _Usage:
    def __init__(self, input_tokens, output_tokens, cached_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_tokens = cached_tokens


class _Response:
    def __init__(self, content, usage):
        self.content = content
        self.usage = usage


class _RecordingProvider:
    def __init__(self, responses):
        self._responses = list(responses)
        self.systems: list[str] = []
        self.messages: list[list[dict]] = []

    def chat(self, messages, tools, system, max_tokens, on_text=None):
        self.systems.append(system)
        self.messages.append([dict(message) for message in messages])
        return self._responses.pop(0)


def _provider(responses) -> _RecordingProvider:
    return _RecordingProvider(responses)


# ============================================================
# system prompt 的稳定性
# ============================================================

def test_system_prompt_identical_across_tool_turns(tmp_path, monkeypatch):
    """一次请求内的多次工具调用，system 必须完全一样。"""
    hooks.clear_hooks()
    provider = _provider([
        _Response(
            [_Block("tool_use", id="call_1", name="glob_files", input={"pattern": "*"})],
            _Usage(10, 5),
        ),
        _Response([_Block("text", text="完成")], _Usage(20, 5)),
    ])
    monkeypatch.setattr(loop, "provider", provider)

    try:
        loop.agent_loop([{"role": "user", "content": "看看目录"}], workspace=tmp_path)
    finally:
        hooks.clear_hooks()

    assert len(provider.systems) == 2
    assert provider.systems[0] == provider.systems[1]
    assert "Long-term Memory" not in provider.systems[0]


def test_build_system_prompt_never_touches_memory_store(monkeypatch):
    """构建 system prompt 时连记忆库都不该被访问 —— 这是结构约束。"""
    from myharness import config

    class ExplodingStore:
        def __getattr__(self, name):
            raise AssertionError(f"build_system_prompt 不该访问 memory_store.{name}")

    monkeypatch.setattr(config, "memory_store", ExplodingStore())
    prompt = config.build_system_prompt()

    assert "Long-term Memory" not in prompt


def test_memory_context_lands_in_user_message_not_system(tmp_path, monkeypatch):
    """端到端：记忆出现在用户消息里，system 里没有。"""
    store = MemoryStore(tmp_path / "memory")
    store._memories.append({
        "name": "indent",
        "description": "user prefers tabs for indentation",
        "type": "user",
        "body": "user prefers tabs for indentation",
        "_path": "",
    })

    hooks.clear_hooks()
    install_default_hooks(store)
    try:
        submitted = hooks.submit_user_prompt("tabs or spaces for indentation?")
        provider = _provider([_Response([_Block("text", text="好")], _Usage(30, 5))])
        monkeypatch.setattr(loop, "provider", provider)

        loop.agent_loop([{"role": "user", "content": submitted}], workspace=tmp_path)
    finally:
        hooks.clear_hooks()

    user_content = provider.messages[0][0]["content"]
    assert "<context>" in user_content
    assert "user prefers tabs for indentation" in user_content
    assert "<context>" not in provider.systems[0]
    assert "user prefers tabs" not in provider.systems[0]


# ============================================================
# 缓存命中：观测字段
# ============================================================

def test_cached_prompt_tokens_supports_three_dialects():
    """Anthropic / OpenAI / DeepSeek 三种字段名都要认。"""
    assert cached_prompt_tokens(SimpleNamespace(cache_read_input_tokens=42)) == 42
    assert cached_prompt_tokens(SimpleNamespace(prompt_cache_hit_tokens=7)) == 7
    assert cached_prompt_tokens(
        SimpleNamespace(prompt_tokens_details=SimpleNamespace(cached_tokens=9))
    ) == 9
    assert cached_prompt_tokens(SimpleNamespace()) == 0


def test_loop_reports_cache_hit(tmp_path, monkeypatch, capsys):
    """命中率必须打印出来，否则前缀有没有稳定只能靠猜。"""
    hooks.clear_hooks()
    provider = _provider([
        _Response([_Block("text", text="好")], _Usage(100, 10, cached_tokens=80)),
    ])
    monkeypatch.setattr(loop, "provider", provider)

    try:
        loop.agent_loop([{"role": "user", "content": "hi"}], workspace=tmp_path)
    finally:
        hooks.clear_hooks()

    out = capsys.readouterr().out
    assert "cache: 80 / 80%" in out
