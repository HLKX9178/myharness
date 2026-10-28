"""测试 M5 — 并发与调度。

验收标准：
1. 任务可在后台执行
2. 按时间触发（cron）
3. 进程重启后状态恢复
4. 幂等与优雅关闭
"""

import json
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
from myharness.runtime.scheduler import (
    Scheduler,
    STATUS_ERROR,
    STATUS_INTERRUPTED,
    STATUS_OK,
    STATUS_RUNNING,
)
from myharness.runtime.persistence import (
    TaskState,
    save_state,
    load_state,
    checkpoint_task,
    get_checkpoint,
)


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    """轮询等待条件成立。

    不用固定 sleep：spawn 出来的工作线程启动时间在不同机器上差很多，
    固定等待在慢机器上就是随机失败。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


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
    """测试定时调度器。

    执行器一律注入假实现：调度逻辑的正确性不该依赖真实模型调用。
    """

    @staticmethod
    def _make(tmp_path, executed: list):
        """构造一个把 prompt 记进 executed 的调度器。"""

        def executor(task):
            executed.append(task.prompt)
            return f"done:{task.name}"

        return Scheduler(tmp_path / "cron.json", executor=executor)

    @staticmethod
    def _make_due(scheduler: Scheduler, task_id: str) -> None:
        """把任务的 next_run 拨到过去，让下一轮 run_pending 必然触发。"""
        with scheduler._lock:
            scheduler.tasks[task_id].next_run = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat()

    def test_schedule_and_execute(self, tmp_path):
        """验收 2：按时间触发（cron），到期执行的是登记时的提示词。"""
        executed: list[str] = []
        scheduler = self._make(tmp_path, executed)

        task_id = scheduler.schedule("test_job", "* * * * *", "say hello")
        self._make_due(scheduler, task_id)

        assert scheduler.run_pending() == [task_id]

        task = scheduler.get(task_id)
        assert _wait_for(lambda: task.last_status == STATUS_OK)
        assert executed == ["say hello"]
        assert task.last_result == "done:test_job"

    def test_thread_executes_due_task(self, tmp_path):
        """调度线程自己会轮询并触发（验收 2 的在线程里的另一半）。"""
        executed: list[str] = []
        scheduler = self._make(tmp_path, executed)

        task_id = scheduler.schedule("threaded", "* * * * *", "tick")
        self._make_due(scheduler, task_id)

        scheduler.start()
        try:
            assert _wait_for(lambda: executed == ["tick"])
        finally:
            scheduler.stop()

    def test_invalid_cron_expression(self, tmp_path):
        """非法 cron 表达式抛出 ValueError。"""
        scheduler = Scheduler(tmp_path / "cron.json")

        with pytest.raises(ValueError, match="Invalid cron expression"):
            scheduler.schedule("bad", "invalid cron", "whatever")

    def test_empty_prompt_rejected(self, tmp_path):
        """空提示词等于白跑一轮模型，登记时就该拒绝。"""
        scheduler = Scheduler(tmp_path / "cron.json")

        with pytest.raises(ValueError, match="prompt"):
            scheduler.schedule("empty", "* * * * *", "   ")

    def test_same_name_updates_instead_of_duplicating(self, tmp_path):
        """同名重复登记是更新，不是再排一条（重试幂等）。"""
        executed: list[str] = []
        scheduler = self._make(tmp_path, executed)

        first = scheduler.schedule("daily", "0 9 * * *", "old prompt")
        second = scheduler.schedule("daily", "0 10 * * *", "new prompt")

        assert first == second
        assert len(scheduler.list_tasks()) == 1
        assert scheduler.get(first).cron_expr == "0 10 * * *"
        assert scheduler.get(first).prompt == "new prompt"

    def test_restart_restores_job_without_registering_anything(self, tmp_path):
        """验收 3：重启后不注册任何 handler 也能跑。

        旧实现把任务存成 func_name，新进程里没重新 register_handler 就只会
        打印 "Handler not found"。提示词随状态文件落盘，这条路径不可能再断。
        """
        first = Scheduler(tmp_path / "cron.json")
        task_id = first.schedule("persist_job", "0 0 * * *", "prompt from before restart")

        executed: list[str] = []

        def executor(task):
            executed.append(task.prompt)

        restarted = Scheduler(tmp_path / "cron.json", executor=executor)
        assert [t.task_id for t in restarted.list_tasks()] == [task_id]

        self._make_due(restarted, task_id)
        restarted.run_pending()

        task = restarted.get(task_id)
        assert _wait_for(lambda: task.last_status == STATUS_OK)
        assert executed == ["prompt from before restart"]

    def test_failure_is_recorded(self, tmp_path):
        """执行失败写进任务状态，而不是抛给调度线程。"""

        def boom(task):
            raise RuntimeError("provider exploded")

        scheduler = Scheduler(tmp_path / "cron.json", executor=boom)
        task_id = scheduler.schedule("fragile", "* * * * *", "do it")
        self._make_due(scheduler, task_id)

        scheduler.run_pending()

        task = scheduler.get(task_id)
        assert _wait_for(lambda: task.last_status == STATUS_ERROR)
        assert "provider exploded" in task.last_error

    def test_disabled_task_is_skipped(self, tmp_path):
        """禁用的任务不会被触发。"""
        executed: list[str] = []
        scheduler = self._make(tmp_path, executed)

        task_id = scheduler.schedule("paused", "* * * * *", "nope", enabled=False)
        self._make_due(scheduler, task_id)

        assert scheduler.run_pending() == []
        assert executed == []

    def test_cancel_by_name_and_id(self, tmp_path):
        """取消接受名称或 ID，重复取消返回 False。"""
        scheduler = Scheduler(tmp_path / "cron.json")
        task_id = scheduler.schedule("cleanup", "0 3 * * *", "clean")

        assert scheduler.cancel(task_id) is True
        assert scheduler.list_tasks() == []
        assert scheduler.cancel("cleanup") is False

        scheduler.schedule("cleanup", "0 3 * * *", "clean")
        assert scheduler.cancel("cleanup") is True

    def test_disable_task_on_cron_error(self, tmp_path):
        """cron 解析失败时任务被禁用，调度不会每轮刷同一个错。"""
        executed: list[str] = []
        scheduler = self._make(tmp_path, executed)
        task_id = scheduler.schedule("broken", "0 0 * * *", "run me")

        # 直接改内存里的表达式，模拟状态文件被手改坏
        with scheduler._lock:
            scheduler.tasks[task_id].cron_expr = "not a cron"
        self._make_due(scheduler, task_id)

        assert scheduler.run_pending() == []
        task = scheduler.get(task_id)
        assert task.enabled is False
        assert task.next_run is None
        assert task.last_status == STATUS_ERROR
        assert executed == []

    def test_state_file_is_valid_json_and_leaves_no_temp(self, tmp_path):
        """状态文件原子写：写完不留 .tmp，内容始终是合法 JSON。"""
        state_file = tmp_path / "cron.json"
        scheduler = Scheduler(state_file)
        scheduler.schedule("a", "0 1 * * *", "first")
        scheduler.schedule("b", "0 2 * * *", "second")

        assert json.loads(state_file.read_text(encoding="utf-8"))
        assert not list(tmp_path.glob("*.tmp"))

    def test_legacy_state_without_prompt_is_skipped(self, tmp_path, capsys):
        """旧格式（func_name）加载时明确跳过并提示，而不是变成哑任务。"""
        state_file = tmp_path / "cron.json"
        state_file.write_text(
            json.dumps(
                {
                    "cron_old_1": {
                        "task_id": "cron_old_1",
                        "name": "old",
                        "cron_expr": "0 0 * * *",
                        "func_name": "job",
                        "args": [],
                        "kwargs": {},
                    }
                }
            ),
            encoding="utf-8",
        )

        scheduler = Scheduler(state_file)

        assert scheduler.list_tasks() == []
        assert "without a prompt" in capsys.readouterr().out

    def test_interrupted_run_is_marked_on_load(self, tmp_path):
        """进程在执行中途被杀：重启后状态不能还停在 running。"""
        state_file = tmp_path / "cron.json"
        assert Scheduler(state_file) is not None

        data = {
            "job_1": {
                "task_id": "job_1",
                "name": "killed",
                "cron_expr": "0 0 * * *",
                "prompt": "work",
                "last_status": STATUS_RUNNING,
                "next_run": (datetime.now() + timedelta(days=1)).isoformat(),
            }
        }
        state_file.write_text(json.dumps(data), encoding="utf-8")

        scheduler = Scheduler(state_file)
        task = scheduler.get("job_1")
        assert task.last_status == STATUS_INTERRUPTED
        assert "Interrupted" in task.last_error


def test_broken_next_run_disables_task(tmp_path):
    """状态文件里 next_run 被改坏：禁用并记录原因，不能每轮刷错。"""
    scheduler = Scheduler(tmp_path / "cron.json")
    task_id = scheduler.schedule("corrupt", "* * * * *", "x")

    with scheduler._lock:
        scheduler.tasks[task_id].next_run = "not-a-date"
        scheduler._save_state()

    assert scheduler.run_pending() == []

    task = scheduler.get(task_id)
    assert task.enabled is False
    assert "Invalid next_run" in task.last_error


def test_daemon_picks_up_jobs_added_by_another_process(tmp_path):
    """常驻调度器要能看见另一个进程登记的任务，且不能把它写没。

    `--run-jobs` 与交互式会话是两个进程，各持一份内存状态。旧行为是
    常驻进程的下一次保存直接覆盖文件 —— 用户刚登记的任务静默消失。
    """
    executed: list[str] = []

    def executor(task):
        executed.append(task.prompt)

    daemon = Scheduler(tmp_path / "cron.json", executor=executor)
    daemon.schedule("existing", "0 3 * * *", "nightly")

    # 另一个进程：加载、登记、保存
    interactive = Scheduler(tmp_path / "cron.json")
    late_id = interactive.schedule("added-later", "* * * * *", "run me")

    daemon.run_pending()  # 本轮没有到期任务，但应当刷新磁盘
    assert {t.name for t in daemon.list_tasks()} == {"existing", "added-later"}

    with daemon._lock:
        daemon.tasks[late_id].next_run = "2000-01-01T00:00:00"
        daemon._save_state()

    daemon.run_pending()

    task = daemon.get(late_id)
    assert _wait_for(lambda: task.last_status == STATUS_OK)
    assert executed == ["run me"]


def test_daemon_drops_jobs_cancelled_by_another_process(tmp_path):
    """另一端取消的任务不会在常驻进程里复活。"""
    daemon = Scheduler(tmp_path / "cron.json")
    task_id = daemon.schedule("temp", "0 3 * * *", "later")

    interactive = Scheduler(tmp_path / "cron.json")
    assert interactive.cancel(task_id) is True

    daemon.run_pending()
    assert daemon.get(task_id) is None


def test_daemon_keeps_running_task_when_merging(tmp_path):
    """对齐磁盘时，正在执行的任务不能被磁盘上的旧状态覆盖掉。"""
    daemon = Scheduler(tmp_path / "cron.json", executor=lambda task: None)
    task_id = daemon.schedule("busy", "* * * * *", "working")

    # 模拟「已经 dispatch、还在执行」
    with daemon._lock:
        daemon.tasks[task_id].last_status = STATUS_RUNNING
        daemon._save_state()

    # 另一个进程写入一个它看不到执行状态的版本
    interactive = Scheduler(tmp_path / "cron.json")
    with interactive._lock:
        interactive.tasks[task_id].last_status = STATUS_OK
        interactive._save_state()

    daemon._refresh_from_disk()

    assert daemon.get(task_id).last_status == STATUS_RUNNING


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
        """调度器把任务提交到后台管理器执行，而不是占住调度线程。"""
        manager = get_manager()
        executed: list[str] = []

        def executor(task):
            executed.append(task.prompt)

        scheduler = Scheduler(tmp_path / "cron.json", executor=executor)
        task_id = scheduler.schedule("integration", "* * * * *", "run in background")

        with scheduler._lock:
            scheduler.tasks[task_id].next_run = (
                datetime.now() - timedelta(seconds=1)
            ).isoformat()

        scheduler.start()
        try:
            assert _wait_for(lambda: executed == ["run in background"])
        finally:
            scheduler.stop()

        cron_tasks = [t for t in manager.list_tasks() if t.name.startswith("cron_")]
        assert len(cron_tasks) >= 1
        assert any(t.status == TaskStatus.COMPLETED for t in cron_tasks)
