"""定时调度器（Cron）。

调度任务是**一段提示词**，不是可调用的 Python 对象。这一点决定了重启之后能不能
恢复：函数在 JSON 里没有可靠的表示（重名、lambda、闭包都无法还原），存个
`func_name` 再指望新进程里恰好有同名函数注册过，等于把恢复能力押在调用方的自觉上
——这正是旧实现重启后只打印 "Handler not found" 的原因。提示词本身就是 payload，
随状态文件一起落盘，新进程读出来就能跑。

职责：
1. 按 cron 表达式登记任务（同名视为更新，重试不会产生重复任务）
2. 原子地持久化任务配置
3. 到期时把任务交给执行器（默认见 ``runtime/runner.py``）
4. 记录每次运行的状态与结果，供 ``list_tasks`` 与排障使用
"""

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from croniter import croniter

from myharness.runtime.background import get_manager

# 任务最近一次运行的状态。running 是「开始执行」时写入的；进程在这一步之后
# 被杀掉，状态就会停在 running —— 加载时把它改判为 interrupted。
STATUS_RUNNING = "running"
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_INTERRUPTED = "interrupted"


@dataclass
class ScheduledTask:
    """一条定时任务。

    ``prompt`` 是任务的全部内容：到期时在一个全新的 agent 会话里作为用户输入
    执行。``workspace`` 记录任务创建时的工作目录，使隔离的 TeamAgent 也能登记
    落在自己目录里的任务。
    """

    task_id: str
    name: str
    cron_expr: str
    prompt: str
    workspace: str | None = None
    enabled: bool = True
    created_at: str = ""
    last_run: str | None = None
    last_status: str | None = None
    last_error: str | None = None
    last_result: str | None = None
    last_session: str | None = None
    next_run: str | None = None


def _log(message: str) -> None:
    """调度器日志。

    常驻进程的输出常被重定向到文件或管道，默认的块缓冲会让日志迟迟不落盘，
    出问题时看不到现场；每次 flush 一行的开销可以忽略。
    """
    print(message, flush=True)


def _next_run_after(cron_expr: str, base: datetime) -> str:
    """计算 base 之后的下一次触发时间。

    Raises:
        ValueError: cron 表达式非法
    """
    try:
        return croniter(cron_expr, base).get_next(datetime).isoformat()
    except Exception as exc:
        raise ValueError(f"Invalid cron expression: {cron_expr}") from exc


class Scheduler:
    """
    定时调度器。

    使用 croniter 解析 cron 表达式，在独立线程中轮询。构造时自动加载已有状态，
    因此进程重启后无需任何额外调用就能接上原来的任务。
    """

    def __init__(
        self,
        state_file: Path,
        executor: Callable[[ScheduledTask], Any] | None = None,
    ):
        """
        初始化调度器。

        Args:
            state_file: 状态持久化文件路径
            executor: 执行器；None 时延迟到首次执行才导入默认实现。
                显式传入使调度器可以脱离模型单独测试。
        """
        self.state_file = Path(state_file)
        self.executor = executor
        self.tasks: dict[str, ScheduledTask] = {}
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._running = False
        # 状态文件最后被谁改过：用于发现别的进程的写入
        self._stamp: int | None = None

        self.load_state()

    # ---------- 登记 / 查询 ----------

    def schedule(
        self,
        name: str,
        cron_expr: str,
        prompt: str,
        workspace: str | Path | None = None,
        enabled: bool = True,
    ) -> str:
        """
        添加或更新一条定时任务。

        以 name 作为身份：同名任务再次登记视为**更新**（改 cron、改提示词、
        重新启用），而不是再排一条。模型重试或人反复执行同一条命令都不会
        攒出一堆重复任务。

        Args:
            name: 任务名称（唯一）
            cron_expr: Cron 表达式（标准 5 字段格式）
            prompt: 到期时交给 agent 的提示词
            workspace: 任务执行时的工作目录；None 表示仓库根
            enabled: 是否启用

        Returns:
            task_id: 任务 ID

        Raises:
            ValueError: cron 表达式非法，或提示词为空
        """
        name = (name or "").strip()
        if not name:
            raise ValueError("Task name must not be empty")

        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("Task prompt must not be empty")

        next_run = _next_run_after(cron_expr, datetime.now())
        workspace_str = str(Path(workspace).resolve()) if workspace else None

        # 先对齐磁盘，否则本次保存会把别的进程刚登记的任务一起写没
        self._refresh_from_disk()

        with self._lock:
            task = self._find(name)
            if task is None:
                task = ScheduledTask(
                    task_id=f"job_{uuid.uuid4().hex[:8]}",
                    name=name,
                    cron_expr=cron_expr,
                    prompt=prompt,
                    workspace=workspace_str,
                    enabled=enabled,
                    created_at=datetime.now().isoformat(),
                    next_run=next_run,
                )
                self.tasks[task.task_id] = task
            else:
                task.cron_expr = cron_expr
                task.prompt = prompt
                task.workspace = workspace_str
                task.enabled = enabled
                task.next_run = next_run
                # 重新登记等于重新开始，上一轮的错误不再代表当前配置
                task.last_error = None

            self._save_state()
            return task.task_id

    def cancel(self, ref: str) -> bool:
        """
        取消任务（接受 task_id 或 name）。

        Args:
            ref: 任务 ID 或名称

        Returns:
            是否真的删掉了任务
        """
        self._refresh_from_disk()

        with self._lock:
            task = self.get(ref)
            if task is None:
                return False
            del self.tasks[task.task_id]
            self._save_state()
            return True

    def get(self, ref: str) -> ScheduledTask | None:
        """按 task_id 或 name 取任务，找不到返回 None。"""
        with self._lock:
            if ref in self.tasks:
                return self.tasks[ref]
            return self._find(ref)

    def list_tasks(self) -> list[ScheduledTask]:
        """
        列出所有调度任务（按下次触发时间排序，未启用的排在最后）。

        Returns:
            任务列表
        """
        with self._lock:
            return sorted(
                self.tasks.values(),
                key=lambda t: (not t.enabled, t.next_run or "9999"),
            )

    def _find(self, name: str) -> ScheduledTask | None:
        """按名称查任务（调用方需持锁）。"""
        for task in self.tasks.values():
            if task.name == name:
                return task
        return None

    # ---------- 生命周期 ----------

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

        已经在后台执行的任务不会被中断（它们在线程池里），调用方若要等它们
        结束，应随后对 ``runtime.background.get_manager()`` 调用 ``shutdown``。

        Args:
            timeout: 等待调度线程退出的超时时间（秒）
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
            try:
                self.run_pending()
            except Exception as exc:
                # 调度线程不能死：一次保存失败或一个坏任务不该让整个调度停摆
                _log(f"[scheduler] Tick failed: {type(exc).__name__}: {exc}")
            self._stop_event.wait(1.0)

    def run_pending(self, now: datetime | None = None) -> list[str]:
        """
        执行所有到期任务。

        与线程解耦，便于测试；``_run_loop`` 每秒调用一次。

        Args:
            now: 当前时间（测试用），None 表示真实时间

        Returns:
            本轮触发并已提交的任务 ID 列表
        """
        now = now or datetime.now()

        # 常驻调度器和交互式会话是两个进程，各自持有一份内存状态。谁后保存谁
        # 覆盖，用户刚登记的任务会静默消失。每轮先对齐磁盘（文件用 os.replace
        # 原子更新，读到的永远是完整版本）。
        self._refresh_from_disk()

        due: list[ScheduledTask] = []
        broken = False

        with self._lock:
            for task in self.tasks.values():
                if not task.enabled or not task.next_run:
                    continue
                raw_next_run = task.next_run
                try:
                    next_run = datetime.fromisoformat(raw_next_run)
                except ValueError:
                    # 状态文件被手改坏了：禁用而不是每轮刷错误
                    task.enabled = False
                    task.next_run = None
                    task.last_status = STATUS_ERROR
                    task.last_error = f"Invalid next_run: {raw_next_run!r}"
                    broken = True
                    continue
                if now >= next_run:
                    due.append(task)

            runnable = [task for task in due if self._advance(task, now)]

            if due or broken:
                self._save_state()

        for task in runnable:
            self._dispatch(task)

        return [task.task_id for task in runnable]

    def _advance(self, task: ScheduledTask, now: datetime) -> bool:
        """把任务推进到下一次触发（调用方需持锁）。

        先推进再执行：即使这一轮执行失败或进程被杀，也不会在同一分钟内
        反复触发同一条任务。

        Returns:
            是否应该执行这一轮（cron 失效的任务会被禁用并返回 False）
        """
        task.last_run = now.isoformat()
        task.last_status = STATUS_RUNNING
        try:
            task.next_run = _next_run_after(task.cron_expr, now)
        except ValueError as exc:
            task.enabled = False
            task.next_run = None
            task.last_status = STATUS_ERROR
            task.last_error = str(exc)
            _log(f"[scheduler] Job disabled, invalid cron: {task.name}")
            return False
        return True

    def _dispatch(self, task: ScheduledTask) -> None:
        """把任务交给后台线程池执行。"""
        executor = self.executor or _default_executor()
        try:
            manager = get_manager()
            manager.submit(
                self._run_and_record,
                executor,
                task,
                name=f"cron_{task.name}",
            )
        except Exception as exc:
            self._record(task, STATUS_ERROR, error=f"Failed to submit: {exc}")

    def _run_and_record(
        self,
        executor: Callable[[ScheduledTask], Any],
        task: ScheduledTask,
    ) -> None:
        """在后台线程里执行任务并记录结果。"""
        try:
            result = executor(task)
        except Exception as exc:
            self._record(task, STATUS_ERROR, error=f"{type(exc).__name__}: {exc}")
            return

        # 执行器可以返回 JobResult（带会话 ID 与输出），也可以直接返回字符串
        if isinstance(result, str):
            session_id, output = None, result
        else:
            session_id = getattr(result, "session_id", None)
            output = getattr(result, "output", None)

        preview = None if output is None else str(output)
        self._record(task, STATUS_OK, result=preview, session_id=session_id)

    def _record(
        self,
        task: ScheduledTask,
        status: str,
        *,
        error: str | None = None,
        result: str | None = None,
        session_id: str | None = None,
    ) -> None:
        """写回一次运行的结果并落盘。

        按 task_id 重新取任务，不用调用方传进来的对象：执行期间可能已经与磁盘
        对齐过，旧对象可能已不在 ``self.tasks`` 里，直接改它就等于丢结果。
        """
        with self._lock:
            current = self.tasks.get(task.task_id)
            if current is None:
                return  # 任务在执行期间被取消
            current.last_status = status
            current.last_error = error
            if result is not None:
                current.last_result = result[:500]
            if session_id:
                current.last_session = session_id
            name = current.name
            self._save_state()
        if status == STATUS_OK:
            _log(f"[scheduler] Job done: {name} (session={session_id or '-'})")
        else:
            _log(f"[scheduler] Job failed: {name}: {error}")

    # ---------- 持久化 ----------

    def _save_state(self) -> None:
        """原子地持久化调度状态（调用方需持锁）。

        直接覆盖写会在「写了一半被杀」时留下半截 JSON，下次启动加载失败、
        所有任务一起消失。先写临时文件再 replace，最坏情况是旧文件仍然完整。
        """
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        data = {task_id: asdict(task) for task_id, task in self.tasks.items()}

        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_file)
        self._stamp = self._file_stamp()

    def _file_stamp(self) -> int | None:
        """状态文件的版本戳（纳秒级 mtime）；文件不存在时返回 None。"""
        try:
            return self.state_file.stat().st_mtime_ns
        except OSError:
            return None

    def _refresh_from_disk(self) -> None:
        """把别的进程写入的变更合并进来。

        常驻的 ``--run-jobs`` 与交互式会话是两个进程：用户在会话里登记的任务，
        常驻进程原先要重启才看得见，而它下一次保存还会把这条任务抹掉。这里按
        task_id 以磁盘为准做一次对齐，但**保留正在执行的任务**的内存状态 ——
        磁盘上它还是 running，不需要覆盖。
        """
        stamp = self._file_stamp()
        if stamp is None or stamp == self._stamp:
            return

        with self._lock:
            disk = self._read_state()
            if disk is None:
                return  # 读失败：宁可继续用内存里的状态，也不清空
            for task_id, task in self.tasks.items():
                if task.last_status == STATUS_RUNNING:
                    # 本进程正在执行它：磁盘上那份（哪怕是别的进程写的 ok）
                    # 不知道这次执行的存在，以内存为准
                    disk[task_id] = task
            self.tasks = disk
            self._stamp = stamp

    def load_state(self) -> None:
        """从磁盘加载调度状态（构造时自动调用）。"""
        loaded = self._read_state()
        if loaded is None:
            return

        with self._lock:
            self.tasks = loaded
            self._stamp = self._file_stamp()

        if loaded:
            _log(f"[scheduler] Loaded {len(loaded)} scheduled tasks")

    def _read_state(self) -> dict[str, ScheduledTask] | None:
        """读状态文件；不存在返回空字典，读坏了返回 None。"""
        if not self.state_file.exists():
            return {}

        try:
            with self.state_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            _log(f"[scheduler] Failed to load state: {exc}")
            return None

        known = {field.name for field in fields(ScheduledTask)}
        loaded: dict[str, ScheduledTask] = {}
        skipped = 0

        for task_id, task_data in data.items():
            if not isinstance(task_data, dict) or "prompt" not in task_data:
                # 旧版本存的是 func_name（可调用对象），无法还原成提示词；
                # 明确报出来，而不是静默变成一条永远执行不了的任务
                skipped += 1
                continue
            clean = {k: v for k, v in task_data.items() if k in known}
            clean.setdefault("task_id", task_id)
            task = ScheduledTask(**clean)
            if task.last_status == STATUS_RUNNING:
                # 上次进程在执行中途退出，结果未知
                task.last_status = STATUS_INTERRUPTED
                task.last_error = "Interrupted: previous process exited while running"
            loaded[task.task_id] = task

        if skipped:
            _log(
                f"[scheduler] Skipped {skipped} task(s) without a prompt "
                f"(state format from an older version)"
            )
        return loaded


# ============================================================
# 全局单例
# ============================================================

_scheduler: Scheduler | None = None


def default_state_file() -> Path:
    """默认状态文件：``.myharness/schedules.json``。

    延迟导入 config：runtime 是纯库，不应该在 import 期就要求模型凭据齐全
    （config 导入时会校验 API key）。
    """
    from myharness.config import SCHEDULE_STATE

    return SCHEDULE_STATE


def _default_executor() -> Callable[[ScheduledTask], Any]:
    """默认执行器：跑一轮 agent（延迟导入，避免循环依赖与凭据早绑）。"""
    from myharness.runtime.runner import execute_task

    return execute_task


def get_scheduler(state_file: Path | None = None) -> Scheduler:
    """
    获取全局调度器（首次调用时不传 state_file 也可用）。

    Args:
        state_file: 状态文件路径；None 时使用 ``.myharness/schedules.json``

    Returns:
        Scheduler 实例（已加载磁盘上的已有任务）
    """
    global _scheduler
    if _scheduler is None:
        _scheduler = Scheduler(state_file or default_state_file())
    return _scheduler


def reset_scheduler() -> None:
    """丢弃全局单例（测试与重新加载用）。"""
    global _scheduler
    _scheduler = None


def schedule_task(
    name: str,
    cron_expr: str,
    prompt: str,
    workspace: str | Path | None = None,
) -> str:
    """
    添加定时任务（便捷函数）。

    Args:
        name: 任务名称
        cron_expr: Cron 表达式
        prompt: 到期时交给 agent 的提示词
        workspace: 任务执行时的工作目录

    Returns:
        task_id: 任务 ID
    """
    return get_scheduler().schedule(name, cron_expr, prompt, workspace=workspace)
