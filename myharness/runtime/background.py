"""后台任务管理器。

职责：
1. 启动后台任务（线程池）
2. 跟踪任务状态（运行中、完成、失败）
3. 取消正在运行的任务
4. 优雅关闭（等待任务完成）
"""

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable


class TaskStatus(Enum):
    """任务状态。"""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskInfo:
    """任务信息。"""
    task_id: str
    name: str
    status: TaskStatus
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    result: Any = None
    error: str | None = None


class BackgroundTaskManager:
    """
    后台任务管理器（单例）。

    使用线程池执行任务，避免阻塞主循环。
    """

    def __init__(self, max_workers: int = 4):
        """
        初始化管理器。

        Args:
            max_workers: 线程池最大工作线程数
        """
        self.executor = ThreadPoolExecutor(max_workers=max_workers)
        self.tasks: dict[str, TaskInfo] = {}
        self.futures: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._shutdown = False

    def submit(
        self,
        func: Callable,
        *args,
        name: str | None = None,
        **kwargs,
    ) -> str:
        """
        提交后台任务。

        Args:
            func: 要执行的函数
            *args: 位置参数
            name: 任务名称（可选）
            **kwargs: 关键字参数

        Returns:
            task_id: 任务 ID

        Raises:
            RuntimeError: 管理器已关闭
        """
        if self._shutdown:
            raise RuntimeError("TaskManager is shut down")

        task_id = str(uuid.uuid4())
        task_name = name or f"{func.__name__}_{task_id[:8]}"

        with self._lock:
            self.tasks[task_id] = TaskInfo(
                task_id=task_id,
                name=task_name,
                status=TaskStatus.PENDING,
                created_at=datetime.now().isoformat(),
            )

        # 提交到线程池
        future = self.executor.submit(self._run_task, task_id, func, args, kwargs)
        self.futures[task_id] = future

        return task_id

    def _run_task(
        self,
        task_id: str,
        func: Callable,
        args: tuple,
        kwargs: dict,
    ) -> None:
        """
        执行任务（在工作线程中）。

        Args:
            task_id: 任务 ID
            func: 要执行的函数
            args: 位置参数
            kwargs: 关键字参数
        """
        with self._lock:
            if task_id in self.tasks:
                self.tasks[task_id].status = TaskStatus.RUNNING
                self.tasks[task_id].started_at = datetime.now().isoformat()

        try:
            result = func(*args, **kwargs)
            with self._lock:
                if task_id in self.tasks:
                    self.tasks[task_id].status = TaskStatus.COMPLETED
                    self.tasks[task_id].result = result
                    self.tasks[task_id].finished_at = datetime.now().isoformat()
        except Exception as exc:
            with self._lock:
                if task_id in self.tasks:
                    self.tasks[task_id].status = TaskStatus.FAILED
                    self.tasks[task_id].error = f"{type(exc).__name__}: {exc}"
                    self.tasks[task_id].finished_at = datetime.now().isoformat()

    def get_status(self, task_id: str) -> TaskInfo | None:
        """
        获取任务状态。

        Args:
            task_id: 任务 ID

        Returns:
            TaskInfo 或 None（任务不存在）
        """
        with self._lock:
            return self.tasks.get(task_id)

    def cancel(self, task_id: str) -> bool:
        """
        取消任务（仅对未开始的任务有效）。

        Args:
            task_id: 任务 ID

        Returns:
            是否成功取消
        """
        future = self.futures.get(task_id)
        if not future:
            return False

        cancelled = future.cancel()
        if cancelled:
            with self._lock:
                if task_id in self.tasks:
                    self.tasks[task_id].status = TaskStatus.CANCELLED
                    self.tasks[task_id].finished_at = datetime.now().isoformat()

        return cancelled

    def list_tasks(self) -> list[TaskInfo]:
        """
        列出所有任务。

        Returns:
            任务列表（按创建时间倒序）
        """
        with self._lock:
            return sorted(
                self.tasks.values(),
                key=lambda t: t.created_at,
                reverse=True,
            )

    def shutdown(self, wait: bool = True, timeout: float | None = None) -> None:
        """
        关闭管理器（优雅关闭）。

        Args:
            wait: 是否等待所有任务完成
            timeout: 等待超时时间（秒，Python 3.12 不支持）
        """
        self._shutdown = True
        self.executor.shutdown(wait=wait)


# 全局单例
_manager: BackgroundTaskManager | None = None


def get_manager() -> BackgroundTaskManager:
    """获取全局后台任务管理器。"""
    global _manager
    if _manager is None:
        _manager = BackgroundTaskManager()
    return _manager


def run_in_background(
    func: Callable,
    *args,
    name: str | None = None,
    **kwargs,
) -> str:
    """
    在后台运行函数（便捷函数）。

    Args:
        func: 要执行的函数
        *args: 位置参数
        name: 任务名称
        **kwargs: 关键字参数

    Returns:
        task_id: 任务 ID
    """
    manager = get_manager()
    return manager.submit(func, *args, name=name, **kwargs)
