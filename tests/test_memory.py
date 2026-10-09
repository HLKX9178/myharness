"""长期记忆测试（M3 验收：跨会话可用，注入量与记忆库大小、会话长度无关）。

Phase 1 重构后改为 .md 文件存储 + MEMORY.md 索引。
"""

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
    MemoryStore(dir_path).remember("项目使用 pytest 作为测试框架", mem_type="project")

    reopened = MemoryStore(dir_path)

    assert len(reopened) == 1
    assert "pytest" in reopened.recall_hook("怎么跑 pytest 测试")


def test_md_files_and_index_are_created(tmp_path):
    dir_path = tmp_path / "memory"
    MemoryStore(dir_path).remember("用户偏好 tabs 缩进", mem_type="user")

    files = list(dir_path.glob("*.md"))
    assert len(files) == 2  # 一个记忆文件 + MEMORY.md 索引
    assert any(f.name == "MEMORY.md" for f in files)

    index = (dir_path / "MEMORY.md").read_text(encoding="utf-8")
    assert "用户偏好 tabs 缩进" in index


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
        store.remember(f"deploy note number {i} about the staging server", mem_type="project")

    injected = store.recall_hook("how do I deploy to staging")

    assert injected is not None
    assert injected.count("\n- ") <= RECALL_LIMIT


def test_unrelated_query_injects_nothing(store):
    store.remember("the staging server runs on port 8080", mem_type="reference")
    assert store.recall_hook("写一首诗") is None


def test_recall_ranks_by_overlap(store):
    store.remember("staging uses postgres", mem_type="project")
    store.remember("staging server port is 8080 and uses nginx", mem_type="project")

    hits = store.search("staging nginx port")

    assert "nginx" in hits[0]["body"]


def test_chinese_query_matches_chinese_memory(store):
    store.remember("用户偏好简洁的中文回复", mem_type="user")
    store.remember("部署脚本在 run.bat", mem_type="reference")

    hits = store.search("回复要简洁一点")

    assert hits and "简洁" in hits[0]["body"]


def test_recall_hook_injects_into_user_message(store):
    """端到端：hook 装配后，检索结果通过 submit_user_prompt 进入用户消息。"""
    store.remember("the staging server port is 8080", mem_type="reference")
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


def test_invalid_type_returns_error(store):
    out = store.remember("fact", mem_type="invalid")
    assert out.startswith("Error:")
    assert "type" in out


def test_duplicate_is_idempotent(store):
    store.remember("fact", mem_type="user")
    out = store.remember("  fact  ", mem_type="user")
    assert not out.startswith("Error:")
    assert len(store) == 1


def test_tool_defs_and_handlers_match(store):
    assert {d["name"] for d in store.tool_defs()} == set(store.handlers()) == {"remember"}
    schema = store.tool_defs()[0]["input_schema"]
    assert "type" in schema["properties"]
    assert set(schema["properties"]["type"]["enum"]) == {"user", "feedback", "project", "reference"}


def test_type_parameter_is_optional(store):
    """type 参数可选，默认 user。"""
    out = store.remember("user preference")
    assert not out.startswith("Error:")
    assert len(store) == 1
    assert store._memories[0]["type"] == "user"


def test_different_types_are_preserved(store):
    store.remember("user fact", mem_type="user")
    store.remember("feedback fact", mem_type="feedback")
    store.remember("project fact", mem_type="project")
    store.remember("reference fact", mem_type="reference")

    assert len(store) == 4
    types = {m["type"] for m in store._memories}
    assert types == {"user", "feedback", "project", "reference"}
