"""Runtime 模块：并发与调度。

提供：
1. 后台任务执行
2. 定时任务（cron）的登记、持久化与执行
3. 任务状态持久化与恢复
4. 优雅关闭
"""

from myharness.runtime.background import BackgroundTaskManager, run_in_background
from myharness.runtime.persistence import TaskState, save_state, load_state
from myharness.runtime.runner import JobResult, execute_task
from myharness.runtime.scheduler import (
    ScheduledTask,
    Scheduler,
    get_scheduler,
    reset_scheduler,
    schedule_task,
)

__all__ = [
    "BackgroundTaskManager",
    "run_in_background",
    "Scheduler",
    "ScheduledTask",
    "schedule_task",
    "get_scheduler",
    "reset_scheduler",
    "JobResult",
    "execute_task",
    "TaskState",
    "save_state",
    "load_state",
]
