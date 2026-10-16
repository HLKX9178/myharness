"""团队工具（tools.py）的回归测试。

这里锁住三类曾经**静默失效**的问题 —— 它们不抛异常、不打日志，
只是永远返回错误结果，因此之前没有任何用例能发现：

1. list_tasks / get_task_result 拿 "in_progress" 去匹配 TaskStatus.CLAIMED
   （真实取值是 "claimed"），状态判定永远落空；
2. get_task_result 的失败分支只读 result，而 fail_task 把原因写在 error；
3. send_team_message 调用了 MessageQueue 上根本不存在的 send_message()。
"""

import pytest

from myharness import tools
from myharness.teams import AgentManager


@pytest.fixture
def active_team(tmp_path, monkeypatch):
    """把 tools 的 `_active_team` 指向一个不启动子进程的 AgentManager。"""
    manager = AgentManager(tmp_path / "team", num_agents=0)
    monkeypatch.setattr(tools, "_active_team", manager)
    return manager


class TestListTasks:
    def test_claimed_task_has_known_status(self, active_team):
        """已认领的任务必须显示为 [IN_PROGRESS]，而不是 [UNKNOWN]。"""
        active_team.taskboard.create_task("写文件", "把 note.txt 写出来")
        active_team.taskboard.claim_task("agent_0")

        out = tools.list_tasks()

        assert "[IN_PROGRESS]" in out
        assert "[UNKNOWN]" not in out
        assert "agent_0" in out

    def test_empty_queue(self, active_team):
        assert tools.list_tasks() == "No tasks in queue"

    def test_failed_task_shows_reason(self, active_team):
        """失败任务的说明在 error 字段，列表里不能只盯着 result。"""
        task_id = active_team.taskboard.create_task("t", "d")
        active_team.taskboard.claim_task("agent_0")
        active_team.taskboard.fail_task(task_id, "agent_0", "RuntimeError: boom")

        out = tools.list_tasks()

        assert "[FAILED]" in out
        assert "RuntimeError: boom" in out


class TestGetTaskResult:
    def test_claimed_task_reports_in_progress(self, active_team):
        task_id = active_team.taskboard.create_task("t", "d")
        active_team.taskboard.claim_task("agent_0")

        out = tools.get_task_result(task_id)

        assert "in progress" in out
        assert "agent_0" in out

    def test_failed_task_surfaces_error(self, active_team):
        task_id = active_team.taskboard.create_task("t", "d")
        active_team.taskboard.claim_task("agent_0")
        active_team.taskboard.fail_task(task_id, "agent_0", "RuntimeError: boom")

        out = tools.get_task_result(task_id)

        assert "failed" in out
        assert "RuntimeError: boom" in out
        assert "no error details" not in out

    def test_completed_task_returns_result(self, active_team):
        task_id = active_team.taskboard.create_task("t", "d")
        active_team.taskboard.claim_task("agent_0")
        active_team.taskboard.complete_task(task_id, "agent_0", "done!")

        assert "done!" in tools.get_task_result(task_id)

    def test_pending_task(self, active_team):
        task_id = active_team.taskboard.create_task("t", "d")

        out = tools.get_task_result(task_id)

        assert "pending" in out

    def test_unknown_task(self, active_team):
        assert "not found" in tools.get_task_result("task_missing")


class TestSendTeamMessage:
    def test_message_reaches_target_inbox(self, active_team):
        """必须真的投递到目标 Agent 的收件箱。"""
        out = tools.send_team_message("agent_1", "hello")

        inbox = active_team.message_queue.list_messages("agent_1")
        assert len(inbox) == 1
        assert inbox[0].message_id in out
        assert inbox[0].content == "hello"
        assert inbox[0].from_agent == "coordinator"

    def test_no_active_team(self, monkeypatch):
        monkeypatch.setattr(tools, "_active_team", None)

        assert tools.send_team_message("agent_1", "hi").startswith("Error:")
