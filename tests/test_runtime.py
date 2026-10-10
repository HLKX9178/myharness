"""测试 M5 — 并发与调度。

验收标准：
1. 任务可在后台执行
2. 按时间触发（cron）
3. 进程重启后状态恢复
4. 幂等与优雅关闭
"""

import time
import pytest
from pathlib import Path
from datetime import datetime, timedelta

from myharness.runtime.background import (
    BackgroundTaskManager,
    TaskStatus,
    run_in_background,
    get_manager,
)
from myharness.runtime.scheduler import Scheduler, ScheduledTask
from myharness.runtime.persistence import (
    TaskState,
    save_state,
    load_state,
    checkpoint_task,
    get_checkpoint,
)


# ==================== 后台任务测试 ====================


class TestBackgroundTaskManager:
    """测试后台任务管理器。"""

    def test_submit_and_complete(self):
        """验收 1：任务可在后台执行。"""
        manager = BackgroundTaskManager(max_workers=2)

        def add(a: int, b: int) -> int:
            return a + b

        task_id = manager.submit(add, 1, 2, name="add_task")

        # 等待完成
        time.sleep(0.1)

        info = manager.get_status(task_id)
        assert info is not None
        assert info.status == TaskStatus.COMPLETED
        assert info.result == 3

        manager.shutdown(wait=False)

    def test_task_failure(self):
        """任务执行失败时，状态为 FAILED 且记录错误。"""
        manager = BackgroundTaskManager(max_workers=2)

        def fail():
            raise ValueError("Test error")

        task_id = manager.submit(fail, name="fail_task")
        time.sleep(0.1)

        info = manager.get_status(task_id)
        assert info is not None
        assert info.status == TaskStatus.FAILED
        assert "ValueError: Test error" in info.error

        manager.shutdown(wait=False)

    def test_cancel_pending_task(self):
        """可以取消未开始的任务。"""
        manager = BackgroundTaskManager(max_workers=1)

        # 先占满线程池
        def block():
            time.sleep(1)

        manager.submit(block, name="blocker")

        # 再提交一个任务（会排队）
        def noop():
            pass

        task_id = manager.submit(noop, name="pending")

        # 立即取消
        cancelled = manager.cancel(task_id)
        assert cancelled is True

        info = manager.get_status(task_id)
        assert info.status == TaskStatus.CANCELLED

        manager.shutdown(wait=False)

    def test_list_tasks(self):
        """可以列出所有任务。"""
        manager = BackgroundTaskManager(max_workers=2)

        def noop():
            pass

        manager.submit(noop, name="task1")
        manager.submit(noop, name="task2")

        time.sleep(0.1)

        tasks = manager.list_tasks()
        assert len(tasks) == 2
        assert tasks[0].name in ["task1", "task2"]

        manager.shutdown(wait=False)

    def test_graceful_shutdown(self):
        """验收 4：优雅关闭 — 等待任务完成。"""
        manager = BackgroundTaskManager(max_workers=2)
        results = []

        def slow_task(n: int):
            time.sleep(0.2)
            results.append(n)

        manager.submit(slow_task, 1)
        manager.submit(slow_task, 2)

        # 优雅关闭（等待完成）
        manager.shutdown(wait=True, timeout=1.0)

        assert len(results) == 2
        assert set(results) == {1, 2}


# ==================== 调度器测试 ====================


class TestScheduler:
    """测试定时调度器。"""

    def test_schedule_and_execute(self, tmp_path):
        """验收 2：按时间触发（cron）。"""
        state_file = tmp_path / "cron.json"
        scheduler = Scheduler(state_file)

        executed = []

        def job(msg: str):
            executed.append(msg)

        # 每秒执行一次（注意：cron 最小粒度是分钟，这里用 * 表示每分钟）
        # 为了测试快速触发，我们手动设置 next_run 为过去时间
        task_id = scheduler.schedule("test_job", "* * * * *", job, "hello")

        # 手动修改 next_run 为过去（触发立即执行）
        with scheduler._lock:
            scheduler.tasks[task_id].next_run = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat()

        # 启动调度器
        scheduler.start()
        time.sleep(2)  # 等待执行
        scheduler.stop()

        assert len(executed) >= 1
        assert executed[0] == "hello"

    def test_invalid_cron_expression(self, tmp_path):
        """非法 cron 表达式抛出 ValueError。"""
        state_file = tmp_path / "cron.json"
        scheduler = Scheduler(state_file)

        def noop():
            pass

        with pytest.raises(ValueError, match="Invalid cron expression"):
            scheduler.schedule("bad", "invalid cron", noop)

    def test_load_state(self, tmp_path):
        """验收 3：进程重启后状态恢复。"""
        state_file = tmp_path / "cron.json"

        # 第一个调度器：添加任务并持久化
        scheduler1 = Scheduler(state_file)

        def job():
            pass

        task_id = scheduler1.schedule("persist_job", "0 0 * * *", job)
        assert state_file.exists()

        # 第二个调度器：加载状态
        scheduler2 = Scheduler(state_file)
        scheduler2.register_handler("job", job)
        scheduler2.load_state()

        tasks = scheduler2.list_tasks()
        assert len(tasks) == 1
        assert tasks[0].task_id == task_id
        assert tasks[0].name == "persist_job"

    def test_disable_task_on_cron_error(self, tmp_path):
        """cron 解析失败时，任务被禁用。"""
        state_file = tmp_path / "cron.json"
        scheduler = Scheduler(state_file)

        def job():
            pass

        task_id = scheduler.schedule("test", "0 0 * * *", job)

        # 手动破坏 cron 表达式
        with scheduler._lock:
            scheduler.tasks[task_id].cron_expr = "invalid"
            scheduler.tasks[task_id].next_run = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat()

        scheduler.start()
        time.sleep(2)
        scheduler.stop()

        # 任务应该被禁用
        info = scheduler.tasks[task_id]
        assert info.enabled is False


# ==================== 持久化测试 ====================


class TestPersistence:
    """测试状态持久化。"""

    def test_save_and_load(self, tmp_path):
        """验收 3：状态可持久化并恢复。"""
        state_file = tmp_path / "tasks.json"

        # 保存状态
        states = {
            "task1": TaskState(
                task_id="task1",
                name="test_task",
                status="completed",
                created_at=datetime.now().isoformat(),
                result="success",
            )
        }
        save_state(state_file, states)

        # 加载状态
        loaded = load_state(state_file)
        assert "task1" in loaded
        assert loaded["task1"].name == "test_task"
        assert loaded["task1"].status == "completed"
        assert loaded["task1"].result == "success"

    def test_load_nonexistent_file(self, tmp_path):
        """加载不存在的文件返回空字典。"""
        state_file = tmp_path / "nonexistent.json"
        loaded = load_state(state_file)
        assert loaded == {}

    def test_checkpoint_and_restore(self, tmp_path):
        """验收 4：幂等 — 可保存和恢复断点。"""
        state_file = tmp_path / "tasks.json"

        # 初始状态
        states = {
            "task1": TaskState(
                task_id="task1",
                name="batch_job",
                status="running",
                created_at=datetime.now().isoformat(),
            )
        }
        save_state(state_file, states)

        # 保存断点
        checkpoint_task(state_file, "task1", {"processed": 50, "total": 100})

        # 恢复断点
        cp = get_checkpoint(state_file, "task1")
        assert cp is not None
        assert cp["processed"] == 50
        assert cp["total"] == 100

    def test_checkpoint_unknown_task(self, tmp_path):
        """给不存在的任务保存断点，打印警告但不崩溃。"""
        state_file = tmp_path / "tasks.json"
        save_state(state_file, {})

        # 不应抛异常
        checkpoint_task(state_file, "unknown", {"data": "test"})

        # 断点不应存在
        cp = get_checkpoint(state_file, "unknown")
        assert cp is None


# ==================== 集成测试 ====================


class TestIntegration:
    """测试 runtime 模块集成。"""

    def test_background_with_global_manager(self):
        """全局管理器可复用。"""
        manager1 = get_manager()
        manager2 = get_manager()
        assert manager1 is manager2

        def add(a, b):
            return a + b

        task_id = run_in_background(add, 1, 2, name="global_task")
        time.sleep(0.1)

        info = manager1.get_status(task_id)
        assert info is not None
        assert info.result == 3

    def test_scheduler_with_background_manager(self, tmp_path):
        """调度器提交任务到后台管理器。"""
        state_file = tmp_path / "cron.json"
        scheduler = Scheduler(state_file)
        manager = get_manager()

        executed = []

        def job(msg: str):
            executed.append(msg)

        # 注册任务
        task_id = scheduler.schedule("integration", "* * * * *", job, "test")

        # 手动触发执行
        with scheduler._lock:
            scheduler.tasks[task_id].next_run = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat()

        scheduler.start()
        time.sleep(2)
        scheduler.stop()

        # 检查后台任务管理器
        tasks = manager.list_tasks()
        cron_tasks = [t for t in tasks if t.name.startswith("cron_")]
        assert len(cron_tasks) >= 1
