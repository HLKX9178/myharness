"""长期记忆测试（M3 验收：跨会话可用，注入量与记忆库大小、会话长度无关）。"""

import json

import pytest

from myharness import hooks
from myharness.bootstrap import install_default_hooks
from myharness.memory import MAX_MEMORY_CHARS, RECALL_LIMIT, MemoryStore


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory" / "memories.json")


@pytest.fixture(autouse=True)
def clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


# ============================================================
# 跨会话
# ============================================================

def test_memory_survives_restart(tmp_path):
    path = tmp_path / "memories.json"
    MemoryStore(path).remember("项目使用 pytest 作为测试框架")

    reopened = MemoryStore(path)

    assert len(reopened) == 1
    assert "pytest" in reopened.recall_hook("怎么跑 pytest 测试")


def test_save_leaves_no_temp_files(tmp_path):
    path = tmp_path / "memories.json"
    MemoryStore(path).remember("fact one")
    assert [p.name for p in tmp_path.iterdir()] == ["memories.json"]


def test_corrupt_file_fails_loudly(tmp_path):
    path = tmp_path / "memories.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="损坏"):
        MemoryStore(path)


# ============================================================
# M3 验收：注入量有硬上限
# ============================================================

def test_injection_bounded_regardless_of_store_size(store):
    """记忆库从 10 条增长到 1000 条，注入条数恒定不超过 RECALL_LIMIT。"""
    for i in range(1000):
        store.remember(f"deploy note number {i} about the staging server")

    injected = store.recall_hook("how do I deploy to staging")

    assert injected is not None
    assert injected.count("\n- ") == RECALL_LIMIT
    assert len(injected) < RECALL_LIMIT * (MAX_MEMORY_CHARS + 50) + 200


def test_unrelated_query_injects_nothing(store):
    store.remember("the staging server runs on port 8080")
    assert store.recall_hook("写一首诗") is None


def test_recall_ranks_by_overlap(store):
    store.remember("staging uses postgres")
    store.remember("staging server port is 8080 and uses nginx")

    hits = store.search("staging nginx port")

    assert "nginx" in hits[0]["content"]


def test_chinese_query_matches_chinese_memory(store):
    store.remember("用户偏好简洁的中文回复")
    store.remember("部署脚本在 run.bat")

    hits = store.search("回复要简洁一点")

    assert hits and "简洁" in hits[0]["content"]


def test_recall_hook_injects_into_user_message(store):
    """端到端：hook 装配后，检索结果通过 submit_user_prompt 进入用户消息。"""
    store.remember("the staging server port is 8080")
    install_default_hooks(store)

    message = hooks.submit_user_prompt("what port does staging use")

    assert message.startswith("<context>")
    assert "8080" in message
    assert message.endswith("what port does staging use")


def test_install_registers_recall_once(store):
    install_default_hooks(store)
    install_default_hooks(store)
    assert hooks.registered_hooks("UserPromptSubmit").count(store.recall_hook) == 1


# ============================================================
# remember 工具契约
# ============================================================

@pytest.mark.parametrize("content", ["", "   ", "x" * (MAX_MEMORY_CHARS + 1)])
def test_invalid_content_returns_error(store, content):
    out = store.remember(content)
    assert out.startswith("Error:")
    assert len(store) == 0


@pytest.mark.parametrize("secret", [
    "the key is sk-ant-abcdefghijklmnopqrstuvwxyz",
    "password: hunter2hunter2",
    "AKIAABCDEFGHIJKLMNOP",
])
def test_secrets_are_refused(store, secret):
    """契约 5 的延伸：密钥不得经由记忆落盘。"""
    out = store.remember(secret)
    assert out.startswith("Error:")
    assert len(store) == 0


def test_duplicate_is_idempotent(store):
    store.remember("fact")
    out = store.remember("  fact  ")
    assert not out.startswith("Error:")
    assert len(store) == 1


def test_write_failure_returns_error_string(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    store = MemoryStore(blocker / "memories.json")

    out = store.remember("fact")

    assert out.startswith("Error:")


def test_tool_defs_and_handlers_match(store):
    assert {d["name"] for d in store.tool_defs()} == set(store.handlers()) == {"remember"}


def test_stored_file_is_utf8_json(tmp_path):
    path = tmp_path / "memories.json"
    MemoryStore(path).remember("中文记忆")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["memories"][0]["content"] == "中文记忆"
