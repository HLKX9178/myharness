"""定时调度器（Cron）。

职责：
1. 按 cron 表达式调度任务
2. 持久化调度配置
3. 进程重启后恢复
4. 支持一次性与循环任务
"""

import json
import threading
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from croniter import croniter

from myharness.runtime.background import get_manager


@dataclass
class ScheduledTask:
    """调度任务配置。"""
    task_id: str
    name: str
    cron_expr: str
    func_name: str  # 函数名（用于序列化）
    args: list[Any]
    kwargs: dict[str, Any]
    enabled: bool = True
    last_run: str | None = None
    next_run: str | None = None


class Scheduler:
    """
    定时调度器。

    使用 croniter 解析 cron 表达式，在独立线程中轮询。
    """

    def __init__(self, state_file: Path):
        """
        初始化调度器。

        Args:
            state_file: 状态持久化文件路径
        """
        self.state_file = state_file
        self.tasks: dict[str, ScheduledTask] = {}
        self.handlers: dict[str, Callable] = {}
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False

    def register_handler(self, name: str, func: Callable) -> None:
        """
        注册可调度的函数。

        Args:
            name: 函数名（唯一标识）
            func: 函数对象
        """
        self.handlers[name] = func

    def schedule(
        self,
        name: str,
        cron_expr: str,
        func: Callable,
        *args,
        **kwargs,
    ) -> str:
        """
        添加调度任务。

        Args:
            name: 任务名称
            cron_expr: Cron 表达式（标准 5 字段格式）
            func: 要执行的函数
            *args: 位置参数
            **kwargs: 关键字参数

        Returns:
            task_id: 任务 ID

        Raises:
            ValueError: cron 表达式非法
        """
        # 验证 cron 表达式
        try:
            iter_obj = croniter(cron_expr, datetime.now())
            next_run = iter_obj.get_next(datetime).isoformat()
        except Exception as exc:
            raise ValueError(f"Invalid cron expression: {cron_expr}") from exc

        # 生成 task_id
        task_id = f"cron_{name}_{int(time.time())}"
        func_name = func.__name__

        # 注册 handler
        self.register_handler(func_name, func)

        # 创建任务
        task = ScheduledTask(
            task_id=task_id,
            name=name,
            cron_expr=cron_expr,
            func_name=func_name,
            args=list(args),
            kwargs=kwargs,
            next_run=next_run,
        )

        with self._lock:
            self.tasks[task_id] = task

        # 持久化
        self._save_state()

        return task_id

    def start(self) -> None:
        """启动调度器（在独立线程中）。"""
        if self._running:
            return

        self._running = True
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """
        停止调度器。

        Args:
            timeout: 等待线程退出的超时时间（秒）
        """
        if not self._running:
            return

        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        self._running = False

    def _run_loop(self) -> None:
        """调度循环（在独立线程中运行）。"""
        while not self._stop_event.is_set():
            now = datetime.now()

            with self._lock:
                tasks_to_run = []
                for task in self.tasks.values():
                    if not task.enabled:
                        continue
                    if not task.next_run:
                        continue

                    next_run_time = datetime.fromisoformat(task.next_run)
                    if now >= next_run_time:
                        tasks_to_run.append(task)

            # 执行到期任务
            for task in tasks_to_run:
                self._execute_task(task)

            # 每秒检查一次
            self._stop_event.wait(1.0)

    def _execute_task(self, task: ScheduledTask) -> None:
        """
        执行调度任务。

        Args:
            task: 调度任务配置
        """
        func = self.handlers.get(task.func_name)
        if not func:
            print(f"[scheduler] Handler not found: {task.func_name}")
            return

        # 提交到后台任务管理器
        manager = get_manager()
        try:
            manager.submit(
                func,
                *task.args,
                name=f"cron_{task.name}",
                **task.kwargs,
            )
        except Exception as exc:
            print(f"[scheduler] Failed to submit task {task.name}: {exc}")

        # 更新时间
        with self._lock:
            task.last_run = datetime.now().isoformat()
            try:
                iter_obj = croniter(task.cron_expr, datetime.now())
                task.next_run = iter_obj.get_next(datetime).isoformat()
            except Exception:
                task.enabled = False
                task.next_run = None

        # 持久化
        self._save_state()

    def _save_state(self) -> None:
        """持久化调度状态到磁盘。"""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {
            task_id: asdict(task)
            for task_id, task in self.tasks.items()
        }
        with self.state_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def load_state(self) -> None:
        """从磁盘加载调度状态（进程重启后恢复）。"""
        if not self.state_file.exists():
            return

        try:
            with self.state_file.open("r", encoding="utf-8") as f:
                data = json.load(f)

            with self._lock:
                self.tasks = {
                    task_id: ScheduledTask(**task_data)
                    for task_id, task_data in data.items()
                }

            print(f"[scheduler] Loaded {len(self.tasks)} scheduled tasks")
        except Exception as exc:
            print(f"[scheduler] Failed to load state: {exc}")

    def list_tasks(self) -> list[ScheduledTask]:
        """
        列出所有调度任务。

        Returns:
            任务列表
        """
        with self._lock:
            return list(self.tasks.values())


# 全局单例
_scheduler: Scheduler | None = None


def get_scheduler(state_file: Path | None = None) -> Scheduler:
    """
    获取全局调度器。

    Args:
        state_file: 状态文件路径（首次调用时必须提供）

    Returns:
        Scheduler 实例
    """
    global _scheduler
    if _scheduler is None:
        if state_file is None:
            raise RuntimeError("state_file must be provided on first call")
        _scheduler = Scheduler(state_file)
    return _scheduler


def schedule_task(
    name: str,
    cron_expr: str,
    func: Callable,
    *args,
    **kwargs,
) -> str:
    """
    添加调度任务（便捷函数）。

    Args:
        name: 任务名称
        cron_expr: Cron 表达式
        func: 要执行的函数
        *args: 位置参数
        **kwargs: 关键字参数

    Returns:
        task_id: 任务 ID
    """
    scheduler = get_scheduler()
    return scheduler.schedule(name, cron_expr, func, *args, **kwargs)
