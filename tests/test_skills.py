"""技能系统测试（M3 验收：按需注入，不随技能数量线性膨胀 prompt）。"""

from pathlib import Path

import pytest

from myharness.skills import BUILTIN_SKILL_DIR, SkillLibrary


def write_skill(directory: Path, name: str, body: str, description: str | None = None) -> None:
    desc = description or f"Use for {name} tasks."
    (directory / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n",
        encoding="utf-8",
    )


# ============================================================
# M3 验收：目录成本与技能正文无关
# ============================================================

def test_catalog_excludes_skill_bodies(tmp_path):
    marker = "BODY-MARKER-should-never-be-in-system-prompt"
    write_skill(tmp_path, "alpha", marker * 100)

    library = SkillLibrary([tmp_path])

    assert "alpha" in library.catalog()
    assert marker not in library.catalog()


def test_catalog_size_independent_of_body_size(tmp_path):
    """正文从 10 字符涨到 100KB，system prompt 中的目录一字不变。"""
    small, large = tmp_path / "small", tmp_path / "large"
    small.mkdir()
    large.mkdir()
    write_skill(small, "alpha", "x" * 10)
    write_skill(large, "alpha", "x" * 100_000)

    assert SkillLibrary([small]).catalog() == SkillLibrary([large]).catalog()


def test_catalog_grows_by_one_line_per_skill(tmp_path):
    """每多一个技能，目录只多一行；增长量由描述决定，与正文无关。"""
    for i in range(50):
        write_skill(tmp_path, f"skill{i:02d}", "y" * 20_000)

    library = SkillLibrary([tmp_path])
    catalog = library.catalog()
    total_bodies = 50 * 20_000

    assert len(catalog.splitlines()) == 2 + 50
    assert len(catalog) < total_bodies / 100


def test_load_skill_returns_full_body(tmp_path):
    write_skill(tmp_path, "alpha", "step one\nstep two")
    out = SkillLibrary([tmp_path]).load_skill("alpha")
    assert "step one\nstep two" in out


# ============================================================
# 工具契约
# ============================================================

def test_unknown_skill_returns_error_string(tmp_path):
    write_skill(tmp_path, "alpha", "body")
    out = SkillLibrary([tmp_path]).load_skill("nope")
    assert out.startswith("Error:")
    assert "alpha" in out, "错误信息应列出可用技能，便于模型自我修正"


def test_load_skill_does_not_touch_filesystem_by_name(tmp_path):
    """名字只用于查表，路径形式的名字不可能读到任意文件。"""
    write_skill(tmp_path, "alpha", "body")
    out = SkillLibrary([tmp_path]).load_skill("../../etc/passwd")
    assert out.startswith("Error:")


def test_tool_defs_and_handlers_match(tmp_path):
    write_skill(tmp_path, "alpha", "body")
    library = SkillLibrary([tmp_path])

    names = {d["name"] for d in library.tool_defs()}
    assert names == set(library.handlers()) == {"load_skill"}
    enum = library.tool_defs()[0]["input_schema"]["properties"]["name"]["enum"]
    assert enum == ["alpha"]


def test_empty_library_exposes_nothing(tmp_path):
    library = SkillLibrary([tmp_path])
    assert library.catalog() == ""
    assert library.tool_defs() == []
    assert library.handlers() == {}


# ============================================================
# 加载期校验：坏技能文件在启动时就失败，而不是运行时静默
# ============================================================

def test_duplicate_skill_name_rejected(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    write_skill(a, "alpha", "one")
    write_skill(b, "alpha", "two")
    with pytest.raises(ValueError, match="重复"):
        SkillLibrary([a, b])


def test_missing_frontmatter_rejected(tmp_path):
    (tmp_path / "bad.md").write_text("no frontmatter", encoding="utf-8")
    with pytest.raises(ValueError, match="frontmatter"):
        SkillLibrary([tmp_path])


def test_overlong_description_rejected(tmp_path):
    write_skill(tmp_path, "alpha", "body", description="d" * 500)
    with pytest.raises(ValueError, match="description"):
        SkillLibrary([tmp_path])


def test_builtin_skills_are_valid():
    library = SkillLibrary([BUILTIN_SKILL_DIR])
    assert {"code_review", "debug"} <= set(library.names())
