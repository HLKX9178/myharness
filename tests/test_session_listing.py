"""会话找回：列表要能认出「哪一条」，恢复要能一步到位。

旧版只有 `id + 更新时间`，38 个存档摆在面前和没列一样 —— 这条路径平时不看，
真丢上下文的时候才用，不能要求人先记住一串时间戳。
"""

import sys
from pathlib import Path

import pytest

from myharness import cli
from myharness.session import Session


def _make_session(
    directory: Path,
    session_id: str,
    *messages: tuple[str, str],
    updated_at: str | None = None,
) -> Session:
    directory.mkdir(parents=True, exist_ok=True)
    session = Session(directory, session_id=session_id)
    for role, content in messages:
        session.add_message(role, content)
    if updated_at:
        session.metadata["updated_at"] = updated_at
    session.save()
    return session


def _dirs(tmp_path) -> tuple[Path, Path]:
    sessions = tmp_path / "sessions"
    scheduled = tmp_path / "scheduled"
    sessions.mkdir()
    scheduled.mkdir()
    return sessions, scheduled


# ============================================================
# Session.list_sessions：预览与条数
# ============================================================

def test_preview_and_count_come_from_the_first_user_message(tmp_path):
    _make_session(
        tmp_path,
        "session_a",
        ("user", "帮我做一个音乐播放器，UI 要好看"),
        ("assistant", "好的"),
    )

    entry = Session.list_sessions(tmp_path)[0]

    assert entry["message_count"] == 2
    assert entry["preview"] == "帮我做一个音乐播放器，UI 要好看"


def test_preview_skips_assistant_and_takes_first_user_turn(tmp_path):
    _make_session(
        tmp_path,
        "session_b",
        ("assistant", "欢迎"),
        ("user", "第二个问题"),
    )

    assert Session.list_sessions(tmp_path)[0]["preview"] == "第二个问题"


def test_preview_is_truncated(tmp_path):
    _make_session(tmp_path, "session_c", ("user", "长" * 200))

    preview = Session.list_sessions(tmp_path)[0]["preview"]
    assert len(preview) == 60


def test_corrupt_session_file_is_skipped(tmp_path):
    _make_session(tmp_path, "session_ok", ("user", "正常会话"))
    (tmp_path / "session_broken.json").write_text("{ not json", encoding="utf-8")

    entries = Session.list_sessions(tmp_path)

    assert [entry["id"] for entry in entries] == ["session_ok"]


def test_sessions_are_sorted_by_update_time(tmp_path):
    _make_session(tmp_path, "session_old", ("user", "旧"), updated_at="2026-10-01T09:00:00")
    _make_session(tmp_path, "session_new", ("user", "新"), updated_at="2026-10-27T18:00:00")

    assert [entry["id"] for entry in Session.list_sessions(tmp_path)] == [
        "session_new",
        "session_old",
    ]


# ============================================================
# --list-sessions：交互会话 + 定时会话，隐藏空壳
# ============================================================

def test_cli_lists_both_sources_and_hides_empty_sessions(tmp_path, monkeypatch, capsys):
    sessions, scheduled = _dirs(tmp_path)
    _make_session(sessions, "session_full", ("user", "列出所有 .py 文件"))
    _make_session(sessions, "session_empty")
    _make_session(scheduled / "job_x", "session_cron", ("user", "每早总结 git log"))

    monkeypatch.setattr(cli, "SESSION_DIR", sessions)
    monkeypatch.setattr(cli, "SCHEDULE_DIR", scheduled)
    monkeypatch.setattr(sys, "argv", ["myharness", "--list-sessions"])

    cli.main()

    out = capsys.readouterr().out
    assert "列出所有 .py 文件" in out
    assert "[定时 job_x]" in out
    assert "session_empty" not in out
    assert "另有 1 条空会话" in out


# ============================================================
# --resume：按 id 或 last
# ============================================================

def test_resume_last_picks_newest_across_sources(tmp_path, monkeypatch):
    sessions, scheduled = _dirs(tmp_path)
    _make_session(sessions, "session_old", ("user", "旧"), updated_at="2026-10-01T09:00:00")
    _make_session(
        scheduled / "job_x",
        "session_new",
        ("user", "新"),
        updated_at="2026-10-27T18:00:00",
    )

    monkeypatch.setattr(cli, "SESSION_DIR", sessions)
    monkeypatch.setattr(cli, "SCHEDULE_DIR", scheduled)

    session = cli._load_session("last")
    assert session.session_id == "session_new"


def test_resume_last_skips_empty_shell(tmp_path, monkeypatch):
    """刚打开又退出会留下一个空会话，last 不该停在它上面。"""
    sessions, scheduled = _dirs(tmp_path)
    _make_session(sessions, "session_real", ("user", "有内容"), updated_at="2026-10-01T09:00:00")
    _make_session(sessions, "session_shell", updated_at="2026-10-27T18:00:00")

    monkeypatch.setattr(cli, "SESSION_DIR", sessions)
    monkeypatch.setattr(cli, "SCHEDULE_DIR", scheduled)

    assert cli._load_session("last").session_id == "session_real"


def test_resume_by_id_finds_scheduled_session(tmp_path, monkeypatch):
    sessions, scheduled = _dirs(tmp_path)
    _make_session(scheduled / "job_x", "session_cron", ("user", "定时里的对话"))

    monkeypatch.setattr(cli, "SESSION_DIR", sessions)
    monkeypatch.setattr(cli, "SCHEDULE_DIR", scheduled)

    session = cli._load_session("session_cron")
    assert session.messages[0]["content"] == "定时里的对话"


def test_resume_unknown_id_raises(tmp_path, monkeypatch):
    sessions, scheduled = _dirs(tmp_path)
    monkeypatch.setattr(cli, "SESSION_DIR", sessions)
    monkeypatch.setattr(cli, "SCHEDULE_DIR", scheduled)

    with pytest.raises(FileNotFoundError):
        cli._load_session("session_does_not_exist")


def test_resume_last_without_sessions_raises(tmp_path, monkeypatch):
    sessions, scheduled = _dirs(tmp_path)
    monkeypatch.setattr(cli, "SESSION_DIR", sessions)
    monkeypatch.setattr(cli, "SCHEDULE_DIR", scheduled)

    with pytest.raises(FileNotFoundError):
        cli._load_session("last")


def test_cli_does_not_create_empty_session(tmp_path, monkeypatch, capsys):
    """打开就退出，不该在 sessions/ 里留下空壳。

    这类空文件是历史遗留的主要来源：39 个存档里 11 个是空的，列表还得分
    一遍再过滤掉。
    """
    sessions, scheduled = _dirs(tmp_path)
    monkeypatch.setattr(cli, "SESSION_DIR", sessions)
    monkeypatch.setattr(cli, "SCHEDULE_DIR", scheduled)
    monkeypatch.setattr(sys, "argv", ["myharness"])
    monkeypatch.setattr("builtins.input", lambda *args: "q")

    cli.main()

    assert list(sessions.glob("session_*.json")) == []
    assert "未产生对话" in capsys.readouterr().out
