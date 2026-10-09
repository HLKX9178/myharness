"""长期记忆测试（M3 验收：跨会话可用，注入量与记忆库大小、会话长度无关）。

Phase 1 重构后改为 .md 文件存储 + MEMORY.md 索引。
Phase 3 改为 Stop hook 自动提取，移除 remember 工具。
"""

from types import SimpleNamespace

import pytest

from myharness import hooks
from myharness.bootstrap import install_default_hooks
from myharness.memory import (
    MAX_MEMORY_CHARS,
    RECALL_LIMIT,
    MemoryStore,
    memory_document,
    memory_slug,
    parse_memory_file,
    rebuild_memory_index,
)


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


@pytest.fixture
def mock_provider():
    """模拟 provider.chat() 返回提取结果。"""
    class MockProvider:
        def __init__(self):
            self.extraction_result = []

        def chat(self, messages, max_tokens=None, **kwargs):
            # 返回预设的提取结果（必须是 JSON 格式）
            import json
            json_text = json.dumps(self.extraction_result)
            return SimpleNamespace(
                content=[SimpleNamespace(text=json_text, type="text")]
            )

    return MockProvider()


@pytest.fixture(autouse=True)
def clean_hooks():
    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


# ============================================================
# 辅助函数
# ============================================================

def test_memory_slug_is_kebab_case():
    assert memory_slug("User Preference: Tabs") == "user-preference-tabs"
    assert memory_slug("中文名称") == "中文名称"
    assert memory_slug("a" * 100) == "a" * 64


def test_memory_document_has_frontmatter():
    doc = memory_document("test", "user", "desc", "body text")
    assert doc.startswith("---\n")
    assert "name: test\n" in doc
    assert "type: user\n" in doc
    assert doc.endswith("body text\n")


def test_parse_memory_file(tmp_path):
    path = tmp_path / "test.md"
    path.write_text(
        "---\nname: test\ndescription: test desc\ntype: user\n---\n\nbody text\n",
        encoding="utf-8",
    )
    mem = parse_memory_file(path)
    assert mem == {
        "name": "test",
        "description": "test desc",
        "type": "user",
        "body": "body text",
    }


def test_parse_rejects_missing_frontmatter(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text("no frontmatter", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        parse_memory_file(path)


def test_parse_rejects_invalid_type(tmp_path):
    path = tmp_path / "bad.md"
    path.write_text(
        "---\nname: x\ndescription: y\ntype: invalid\n---\n\nbody\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="type 必须是"):
        parse_memory_file(path)


# ============================================================
# 跨会话
# ============================================================

def test_memory_survives_restart(tmp_path):
    dir_path = tmp_path / "memory"
    store = MemoryStore(dir_path)
    # Phase 3：直接写入，不再通过 remember 工具
    store._memories.append({
        "name": "pytest fact",
        "description": "pytest fact",
        "type": "project",
        "body": "项目使用 pytest 作为测试框架",
        "_path": str(dir_path / "test.md"),
    })
    (dir_path / "test.md").write_text(
        memory_document("pytest fact", "project", "pytest fact", "项目使用 pytest 作为测试框架"),
        encoding="utf-8",
    )
    rebuild_memory_index(dir_path)

    reopened = MemoryStore(dir_path)

    assert len(reopened) == 1
    assert "pytest" in reopened.recall_hook("怎么跑 pytest 测试")


def test_md_files_and_index_are_created(tmp_path):
    dir_path = tmp_path / "memory"
    store = MemoryStore(dir_path)
    # 直接写入测试
    path = dir_path / "test.md"
    path.write_text(
        memory_document("tabs", "user", "tabs", "用户偏好 tabs 缩进"),
        encoding="utf-8",
    )
    rebuild_memory_index(dir_path)

    files = list(dir_path.glob("*.md"))
    assert len(files) == 2  # 一个记忆文件 + MEMORY.md 索引
    assert any(f.name == "MEMORY.md" for f in files)

    index = (dir_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "tabs" in index


def test_corrupt_file_does_not_block_startup(tmp_path):
    dir_path = tmp_path / "memory"
    dir_path.mkdir()
    (dir_path / "bad.md").write_text("not valid frontmatter", encoding="utf-8")

    # 启动时跳过坏文件，不抛异常
    store = MemoryStore(dir_path)
    assert len(store) == 0

    # 索引中标注了损坏的文件
    index = (dir_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "⚠️" in index or "parse error" in index


def test_rebuild_index_after_manual_file_changes(tmp_path):
    dir_path = tmp_path / "memory"
    dir_path.mkdir()
    (dir_path / "manual.md").write_text(
        "---\nname: manual\ndescription: manual desc\ntype: feedback\n---\n\nmanual body\n",
        encoding="utf-8",
    )

    rebuild_memory_index(dir_path)

    index = (dir_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "manual" in index


# ============================================================
# M3 验收：注入量有硬上限
# ============================================================

def test_injection_bounded_regardless_of_store_size(store):
    """记忆库从 10 条增长到 1000 条，注入条数恒定不超过 RECALL_LIMIT。"""
    for i in range(50):
        # 直接写入，不通过工具
        store._memories.append({
            "name": f"note{i}",
            "description": f"note{i}",
            "type": "project",
            "body": f"deploy note number {i} about the staging server",
            "_path": "",
        })

    injected = store.recall_hook("how do I deploy to staging")

    assert injected is not None
    assert injected.count("\n- ") <= RECALL_LIMIT


def test_unrelated_query_injects_nothing(store):
    store._memories.append({
        "name": "port",
        "description": "port",
        "type": "reference",
        "body": "the staging server runs on port 8080",
        "_path": "",
    })
    assert store.recall_hook("写一首诗") is None


def test_recall_ranks_by_overlap(store):
    store._memories.extend([
        {"name": "pg", "description": "pg", "type": "project", "body": "staging uses postgres", "_path": ""},
        {"name": "nginx", "description": "nginx", "type": "project", "body": "staging server port is 8080 and uses nginx", "_path": ""},
    ])

    hits = store.search("staging nginx port")

    assert "nginx" in hits[0]["body"]


def test_chinese_query_matches_chinese_memory(store):
    store._memories.extend([
        {"name": "简洁", "description": "简洁", "type": "user", "body": "用户偏好简洁的中文回复", "_path": ""},
        {"name": "部署", "description": "部署", "type": "reference", "body": "部署脚本在 run.bat", "_path": ""},
    ])

    hits = store.search("回复要简洁一点")

    assert hits and "简洁" in hits[0]["body"]


def test_recall_hook_injects_into_user_message(store):
    """端到端：hook 装配后，检索结果通过 submit_user_prompt 进入用户消息。"""
    store._memories.append({
        "name": "port",
        "description": "port 8080",
        "type": "reference",
        "body": "the staging server port is 8080",
        "_path": "",
    })
    install_default_hooks(store)

    message = hooks.submit_user_prompt("what port does staging use")

    assert message.startswith("<context>")
    assert "8080" in message or "staging" in message
    assert message.endswith("what port does staging use")


def test_install_registers_recall_once(store):
    install_default_hooks(store)
    install_default_hooks(store)
    assert hooks.registered_hooks("UserPromptSubmit").count(store.recall_hook) == 1


# ============================================================
# Phase 3：自动提取
# ============================================================

def test_should_store_memory_rejects_current_task(store):
    candidate = {"content": "do not create files", "type": "user", "scope": "current_task"}
    ok, reason = store.should_store_memory(candidate)
    assert not ok
    assert "current_task" in reason


def test_should_store_memory_rejects_transient_expressions(store):
    candidate = {"content": "这次不要用 spaces", "type": "user", "scope": "persistent"}
    ok, reason = store.should_store_memory(candidate)
    assert not ok
    assert "transient" in reason


def test_should_store_memory_rejects_secrets(store):
    candidate = {"content": "api key is sk-ant-abcdefghijklmnopqrstuvwxyz1234", "type": "user", "scope": "persistent"}
    ok, reason = store.should_store_memory(candidate)
    assert not ok
    assert "credential" in reason


def test_should_store_memory_rejects_similar(store):
    store._memories.append({
        "name": "tabs",
        "description": "tabs",
        "type": "user",
        "body": "user prefers tabs for indentation",
        "_path": "",
    })
    candidate = {"content": "user prefers tabs indentation", "type": "user", "scope": "persistent"}
    ok, reason = store.should_store_memory(candidate)
    assert not ok
    assert "similar" in reason


def test_should_store_memory_accepts_valid(store):
    candidate = {
        "content": "项目使用 Conventional Commits 格式",
        "type": "project",
        "scope": "persistent",
    }
    ok, reason = store.should_store_memory(candidate)
    assert ok
    assert reason == ""


def test_extract_memories_hook_writes_to_disk(store, mock_provider):
    mock_provider.extraction_result = [
        {"content": "user prefers tabs", "type": "user", "scope": "persistent"}
    ]

    messages = [
        {"role": "user", "content": "I prefer tabs for indentation. Remember that."},
        {"role": "assistant", "content": "OK"},
    ]

    store.extract_memories_hook(messages, mock_provider)

    assert len(store) == 1
    assert "tabs" in store._memories[0]["body"]
    files = list(store.memory_dir.glob("*.md"))
    assert len(files) == 2  # 记忆文件 + 索引


def test_extract_memories_hook_skips_current_task(store, mock_provider):
    mock_provider.extraction_result = [
        {"content": "this time use spaces", "type": "user", "scope": "current_task"}
    ]

    messages = [
        {"role": "user", "content": "use spaces this time"},
        {"role": "assistant", "content": "OK"},
    ]

    store.extract_memories_hook(messages, mock_provider)

    assert len(store) == 0


def test_extract_memories_hook_handles_json_parse_error(store, mock_provider):
    mock_provider.extraction_result = "not json"

    messages = [
        {"role": "user", "content": "test"},
        {"role": "assistant", "content": "OK"},
    ]

    # 不抛异常，静默忽略
    store.extract_memories_hook(messages, None)
    assert len(store) == 0


def test_tools_no_longer_include_remember(store):
    """Phase 3：remember 工具已移除，改为自动提取。"""
    assert store.tool_defs() == []
    assert store.handlers() == {}
