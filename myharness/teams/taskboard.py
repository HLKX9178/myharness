"""TaskBoard：共享任务队列。

核心功能：
1. 任务创建与持久化（JSON Lines 格式）
2. 原子 claim 操作（单个 Agent 认领任务）
3. 超时检测与重新认领（崩溃恢复）
4. 任务状态查询

设计约束：
- 所有状态变更必须立即写入磁盘（容错）
- claim 操作必须是原子的（避免竞态）
- 超时任务能被其他 Agent 接管
"""

import json
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from myharness.teams.filelock import FileLock


class TaskStatus(Enum):
    """任务状态。"""
    PENDING = "pending"      # 等待认领
    CLAIMED = "claimed"      # 已认领，执行中
    COMPLETED = "completed"  # 已完成
    FAILED = "failed"        # 失败


@dataclass
class Task:
    """任务数据结构。"""
    task_id: str
    title: str
    description: str
    status: TaskStatus
    created_at: str
    claimed_by: str | None = None      # 认领者 Agent ID
    claimed_at: str | None = None      # 认领时间
    completed_at: str | None = None
    result: Any | None = None
    error: str | None = None


class TaskBoard:
    """
    共享任务队列（多 Agent 协作）。

    持久化格式：JSON Lines，每行一个任务的最新状态。

    并发模型：
    - **进程内**用 ``threading.Lock`` 互斥（同进程多线程共享一个实例）。
    - **跨进程**用 ``FileLock``（``.locks/<hash>.lock``）互斥。worker 是
      ``spawn`` 出来的独立进程，各自构造自己的 TaskBoard，进程内的锁对它们
      毫无约束 —— 早期版本只有进程内锁，两个 worker 会同时读到同一个 PENDING
      任务、各自写入 CLAIMED，于是同一任务被执行两次、工作区被写两遍。
    - 所有「读 → 改 → 写」都必须在同一个 ``_guard()`` 里完成，否则仍是
      TOCTOU 竞态。
    """

    def __init__(
        self,
        board_file: Path,
        timeout_seconds: int = 300,
        lock_timeout: float = 10.0,
    ):
        """
        初始化任务板。

        Args:
            board_file: 任务板文件路径（JSON Lines）
            timeout_seconds: 任务超时时间（秒）。超时后任务可被重新认领。
            lock_timeout: 跨进程锁的最长等待时间（秒）
        """
        self.board_file = Path(board_file)
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._lock_timeout = lock_timeout
        self._ensure_file()

    @contextmanager
    def _guard(self) -> Iterator[None]:
        """进程内 + 跨进程双重互斥。

        两层锁缺一不可，所以顺序固定为「先进程内、后跨进程」：

        - 只有进程内锁 → 独立进程之间完全不互斥（本 bug 的成因）。
        - 只有文件锁 → 同进程的多个线程会在文件锁上互相等待，本该并行的
          读操作被串行化；而且 flock 的互斥粒度是 open file description，
          对 fork 出来的子进程不成立，靠不住。
        """
        with self._lock:
            with FileLock(
                self.board_file.parent,
                self.board_file.name,
                timeout=self._lock_timeout,
            ):
                yield

    def _ensure_file(self) -> None:
        """确保文件存在。

        用 ``touch(exist_ok=True)`` 而不是 ``write_text("")``：后者是截断写，
        两个 worker 同时启动、都判断到文件不存在时，后到的那个会把对方
        刚建好的任务板清空。
        """
        self.board_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.board_file.exists():
            self.board_file.touch(exist_ok=True)

    def create_task(self, title: str, description: str) -> str:
        """
        创建新任务。

        Args:
            title: 任务标题
            description: 任务描述

        Returns:
            task_id: 任务 ID
        """
        task_id = f"task_{uuid.uuid4().hex[:12]}"

        task = Task(
            task_id=task_id,
            title=title,
            description=description,
            status=TaskStatus.PENDING,
            created_at=datetime.now().isoformat(),
        )

        with self._guard():
            self._append_task(task)
        return task_id

    def claim_task(self, agent_id: str) -> Task | None:
        """
        认领一个待处理任务（原子操作）。

        规则：
        1. 优先认领 PENDING 任务
        2. 如果没有 PENDING，查找超时的 CLAIMED 任务
        3. 返回认领成功的任务，如果没有可用任务返回 None

        Args:
            agent_id: Agent ID

        Returns:
            认领成功的任务，或 None
        """
        # 读、选、写必须在同一把锁内：拆开做就是 TOCTOU ——
        # 两个 worker 都读到 PENDING、都认为该由自己执行。
        with self._guard():
            tasks = self._load_all_tasks()

            # 1. 优先认领 PENDING 任务
            for task in tasks.values():
                if task.status == TaskStatus.PENDING:
                    task.status = TaskStatus.CLAIMED
                    task.claimed_by = agent_id
                    task.claimed_at = datetime.now().isoformat()
                    self._append_task(task)
                    return task

            # 2. 查找超时的 CLAIMED 任务
            now = datetime.now()
            for task in tasks.values():
                if task.status == TaskStatus.CLAIMED and task.claimed_at:
                    claimed_time = datetime.fromisoformat(task.claimed_at)
                    if now - claimed_time > timedelta(seconds=self.timeout_seconds):
                        # 超时，重新认领
                        task.claimed_by = agent_id
                        task.claimed_at = datetime.now().isoformat()
                        self._append_task(task)
                        return task

            return None

    def complete_task(
        self,
        task_id: str,
        agent_id: str,
        result: Any = None,
    ) -> bool:
        """
        标记任务为完成。

        Args:
            task_id: 任务 ID
            agent_id: Agent ID（必须是认领者）
            result: 任务结果

        Returns:
            是否成功完成
        """
        with self._guard():
            tasks = self._load_all_tasks()
            task = tasks.get(task_id)

            if not task:
                return False
            if task.claimed_by != agent_id:
                # 不是认领者，无法完成
                return False
            if task.status != TaskStatus.CLAIMED:
                return False

            task.status = TaskStatus.COMPLETED
            task.completed_at = datetime.now().isoformat()
            task.result = result
            self._append_task(task)
            return True

    def fail_task(
        self,
        task_id: str,
        agent_id: str,
        error: str,
    ) -> bool:
        """
        标记任务为失败。

        Args:
            task_id: 任务 ID
            agent_id: Agent ID（必须是认领者）
            error: 错误信息

        Returns:
            是否成功标记
        """
        with self._guard():
            tasks = self._load_all_tasks()
            task = tasks.get(task_id)

            if not task:
                return False
            if task.claimed_by != agent_id:
                return False
            if task.status != TaskStatus.CLAIMED:
                return False

            task.status = TaskStatus.FAILED
            task.completed_at = datetime.now().isoformat()
            task.error = error
            self._append_task(task)
            return True

    def get_task(self, task_id: str) -> Task | None:
        """
        获取任务详情。

        Args:
            task_id: 任务 ID

        Returns:
            Task 或 None
        """
        with self._guard():
            tasks = self._load_all_tasks()
            return tasks.get(task_id)

    def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        """
        列出任务。

        Args:
            status: 过滤状态（None 表示全部）

        Returns:
            任务列表（按创建时间倒序）
        """
        with self._guard():
            tasks = self._load_all_tasks()
            result = list(tasks.values())

            if status:
                result = [t for t in result if t.status == status]

            return sorted(result, key=lambda t: t.created_at, reverse=True)

    def _load_all_tasks(self) -> dict[str, Task]:
        """
        从磁盘加载所有任务（最新状态）。

        JSON Lines 格式：每行一个任务的状态快照。
        同一个 task_id 可能出现多次（状态变更历史）。
        只保留最后一次出现的状态。

        Returns:
            {task_id: Task} 映射
        """
        tasks: dict[str, Task] = {}

        if not self.board_file.exists():
            return tasks

        try:
            with self.board_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    # 逐行容错：一行坏了（历史遗留的半行、手工编辑出错）
                    # 不该把整个任务板判死刑 —— 早期实现把 try 套在整个
                    # 循环外面，一个坏行会让之后所有任务凭空消失。
                    try:
                        data = json.loads(line)
                        # 恢复 Enum
                        data["status"] = TaskStatus(data["status"])
                        task = Task(**data)
                    except (json.JSONDecodeError, KeyError, ValueError) as exc:
                        print(f"[taskboard] Skipping malformed line: {exc}")
                        continue
                    tasks[task.task_id] = task
        except OSError as exc:
            print(f"[taskboard] Failed to load tasks: {exc}")

        return tasks

    def _append_task(self, task: Task) -> None:
        """
        追加任务状态到文件（append-only）。

        调用方必须持有 ``self._guard()``：这里只保证「一行写完」，
        不负责与其他进程的写入排序。

        Args:
            task: 任务对象
        """
        data = asdict(task)
        # Enum 转字符串
        data["status"] = task.status.value

        with self.board_file.open("a", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.write("\n")
