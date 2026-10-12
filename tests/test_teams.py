"""M6 — 多 Agent 协作测试。

测试范围：
1. TaskBoard：任务创建、认领、完成、超时接管
2. MessageQueue：消息发送、接收、确认
3. FileLock：并发文件锁定
4. TeamAgent：独立工作目录、任务执行、消息传递
"""

import json
import tempfile
import threading
import time
from pathlib import Path

import pytest

from myharness.teams import (
    TaskBoard,
    TaskStatus,
    MessageQueue,
    MessageStatus,
    FileLock,
    TeamAgent,
)


# ============================================================
# TaskBoard 测试
# ============================================================

class TestTaskBoard:
    """TaskBoard 功能测试。"""

    def test_create_task(self, tmp_path):
        """测试：创建任务。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        task_id = board.create_task("Test Task", "Do something")

        assert task_id.startswith("task_")
        task = board.get_task(task_id)
        assert task is not None
        assert task.title == "Test Task"
        assert task.status == TaskStatus.PENDING

    def test_claim_task(self, tmp_path):
        """测试：认领任务。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        task_id = board.create_task("Task 1", "Description")

        # Agent A 认领
        task = board.claim_task("agent_a")
        assert task is not None
        assert task.task_id == task_id
        assert task.status == TaskStatus.CLAIMED
        assert task.claimed_by == "agent_a"

        # Agent B 无法认领（已被认领）
        task2 = board.claim_task("agent_b")
        assert task2 is None

    def test_complete_task(self, tmp_path):
        """测试：完成任务。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        task_id = board.create_task("Task 1", "Description")

        board.claim_task("agent_a")
        success = board.complete_task(task_id, "agent_a", "Done!")

        assert success is True
        task = board.get_task(task_id)
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "Done!"

    def test_complete_task_wrong_agent(self, tmp_path):
        """测试：非认领者无法完成任务。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        task_id = board.create_task("Task 1", "Description")

        board.claim_task("agent_a")
        success = board.complete_task(task_id, "agent_b", "Hacked!")

        assert success is False
        task = board.get_task(task_id)
        assert task.status == TaskStatus.CLAIMED  # 状态未改变

    def test_fail_task(self, tmp_path):
        """测试：标记任务失败。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        task_id = board.create_task("Task 1", "Description")

        board.claim_task("agent_a")
        success = board.fail_task(task_id, "agent_a", "Error occurred")

        assert success is True
        task = board.get_task(task_id)
        assert task.status == TaskStatus.FAILED
        assert task.error == "Error occurred"

    def test_timeout_reclaim(self, tmp_path):
        """测试：超时任务可被重新认领。"""
        board = TaskBoard(tmp_path / "board.jsonl", timeout_seconds=1)
        task_id = board.create_task("Task 1", "Description")

        # Agent A 认领
        board.claim_task("agent_a")

        # 等待超时
        time.sleep(1.5)

        # Agent B 重新认领
        task = board.claim_task("agent_b")
        assert task is not None
        assert task.task_id == task_id
        assert task.claimed_by == "agent_b"

    def test_list_tasks(self, tmp_path):
        """测试：列出任务。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        board.create_task("Task 1", "Desc 1")
        board.create_task("Task 2", "Desc 2")
        board.create_task("Task 3", "Desc 3")

        board.claim_task("agent_a")

        all_tasks = board.list_tasks()
        assert len(all_tasks) == 3

        pending = board.list_tasks(status=TaskStatus.PENDING)
        assert len(pending) == 2

        claimed = board.list_tasks(status=TaskStatus.CLAIMED)
        assert len(claimed) == 1

    def test_persistence(self, tmp_path):
        """测试：任务持久化。"""
        board_file = tmp_path / "board.jsonl"
        board1 = TaskBoard(board_file)
        task_id = board1.create_task("Task 1", "Description")
        board1.claim_task("agent_a")

        # 重新加载
        board2 = TaskBoard(board_file)
        task = board2.get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.CLAIMED
        assert task.claimed_by == "agent_a"


# ============================================================
# MessageQueue 测试
# ============================================================

class TestMessageQueue:
    """MessageQueue 功能测试。"""

    def test_send_and_receive(self, tmp_path):
        """测试：发送和接收消息。"""
        queue = MessageQueue(tmp_path / "queue")
        msg_id = queue.send("agent_a", "agent_b", "Hello!")

        assert msg_id.startswith("msg_")

        # Agent B 接收
        messages = queue.receive("agent_b")
        assert len(messages) == 1
        assert messages[0].message_id == msg_id
        assert messages[0].content == "Hello!"
        assert messages[0].status == MessageStatus.DELIVERED

    def test_ack_message(self, tmp_path):
        """测试：确认消息。"""
        queue = MessageQueue(tmp_path / "queue")
        msg_id = queue.send("agent_a", "agent_b", "Hello!")

        queue.receive("agent_b")
        success = queue.ack("agent_b", msg_id)

        assert success is True

        # 确认后状态变为 ACKED
        messages = queue.list_messages("agent_b", status=MessageStatus.ACKED)
        assert len(messages) == 1

    def test_receive_limit(self, tmp_path):
        """测试：接收消息数量限制。"""
        queue = MessageQueue(tmp_path / "queue")
        for i in range(5):
            queue.send("agent_a", "agent_b", f"Message {i}")

        messages = queue.receive("agent_b", limit=3)
        assert len(messages) == 3

    def test_fifo_order(self, tmp_path):
        """测试：FIFO 顺序。"""
        queue = MessageQueue(tmp_path / "queue")
        queue.send("agent_a", "agent_b", "First")
        queue.send("agent_a", "agent_b", "Second")
        queue.send("agent_a", "agent_b", "Third")

        messages = queue.receive("agent_b")
        assert messages[0].content == "First"
        assert messages[1].content == "Second"
        assert messages[2].content == "Third"

    def test_persistence(self, tmp_path):
        """测试：消息持久化。"""
        queue_dir = tmp_path / "queue"
        queue1 = MessageQueue(queue_dir)
        msg_id = queue1.send("agent_a", "agent_b", "Hello!")

        # 重新加载
        queue2 = MessageQueue(queue_dir)
        messages = queue2.receive("agent_b")
        assert len(messages) == 1
        assert messages[0].message_id == msg_id


# ============================================================
# FileLock 测试
# ============================================================

class TestFileLock:
    """FileLock 功能测试。"""

    def test_acquire_and_release(self, tmp_path):
        """测试：获取和释放锁。"""
        lock = FileLock(tmp_path, "test_file.txt")
        lock.acquire()
        lock.release()

    def test_context_manager(self, tmp_path):
        """测试：上下文管理器。"""
        with FileLock(tmp_path, "test_file.txt"):
            # 锁定期间
            pass
        # 锁已释放

    def test_concurrent_access_blocked(self, tmp_path):
        """测试：并发访问被阻塞。"""
        results = []

        def task1():
            with FileLock(tmp_path, "shared_file.txt", timeout=2.0):
                results.append("task1_start")
                time.sleep(0.5)
                results.append("task1_end")

        def task2():
            time.sleep(0.1)  # 确保 task1 先获取锁
            try:
                with FileLock(tmp_path, "shared_file.txt", timeout=0.2):
                    results.append("task2_acquired")
            except TimeoutError:
                results.append("task2_timeout")

        t1 = threading.Thread(target=task1)
        t2 = threading.Thread(target=task2)

        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert "task1_start" in results
        assert "task1_end" in results
        assert "task2_timeout" in results
        assert "task2_acquired" not in results

    def test_timeout(self, tmp_path):
        """测试：获取锁超时。"""
        lock1 = FileLock(tmp_path, "test_file.txt")
        lock1.acquire()

        lock2 = FileLock(tmp_path, "test_file.txt", timeout=0.5)
        with pytest.raises(TimeoutError):
            lock2.acquire()

        lock1.release()


# ============================================================
# TeamAgent 测试
# ============================================================

class TestTeamAgent:
    """TeamAgent 功能测试。"""

    def test_initialization(self, tmp_path):
        """测试：Agent 初始化。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        queue = MessageQueue(tmp_path / "queue")

        agent = TeamAgent("agent_1", tmp_path, board, queue)

        assert agent.agent_id == "agent_1"
        assert agent.workspace.exists()
        assert agent.session_dir.exists()

    def test_send_and_receive_messages(self, tmp_path):
        """测试：Agent 间消息传递。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        queue = MessageQueue(tmp_path / "queue")

        agent_a = TeamAgent("agent_a", tmp_path, board, queue)
        agent_b = TeamAgent("agent_b", tmp_path, board, queue)

        # Agent A 发送消息给 Agent B
        msg_id = agent_a.send_message("agent_b", "Hello from A!")

        # Agent B 接收消息
        messages = agent_b.receive_messages()
        assert len(messages) == 1
        assert messages[0].content == "Hello from A!"

    def test_workspace_isolation(self, tmp_path):
        """测试：工作目录隔离。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        queue = MessageQueue(tmp_path / "queue")

        agent_a = TeamAgent("agent_a", tmp_path, board, queue)
        agent_b = TeamAgent("agent_b", tmp_path, board, queue)

        assert agent_a.workspace != agent_b.workspace
        assert agent_a.workspace.exists()
        assert agent_b.workspace.exists()

    def test_get_status(self, tmp_path):
        """测试：获取 Agent 状态。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        queue = MessageQueue(tmp_path / "queue")

        agent = TeamAgent("agent_1", tmp_path, board, queue)
        status = agent.get_status()

        assert status["agent_id"] == "agent_1"
        assert "workspace" in status
        assert "session_messages" in status

    def test_file_isolation_behavior(self, tmp_path):
        """测试：验证文件操作真正隔离（行为测试）。"""
        board = TaskBoard(tmp_path / "board.jsonl")
        queue = MessageQueue(tmp_path / "queue")

        agent_a = TeamAgent("agent_a", tmp_path, board, queue)
        agent_b = TeamAgent("agent_b", tmp_path, board, queue)

        # Agent A 在自己的工作目录写文件
        file_a = agent_a.workspace / "test.txt"
        file_a.write_text("Agent A was here")

        # Agent B 在自己的工作目录写同名文件
        file_b = agent_b.workspace / "test.txt"
        file_b.write_text("Agent B was here")

        # 验证：两个文件独立存在，内容不同
        assert file_a.exists()
        assert file_b.exists()
        assert file_a.read_text() == "Agent A was here"
        assert file_b.read_text() == "Agent B was here"
        assert file_a != file_b  # 物理路径不同
