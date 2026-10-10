"""Runtime 模块：并发与调度。

提供：
1. 后台任务执行
2. 定时调度（cron）
3. 状态持久化与恢复
4. 优雅关闭
"""

from myharness.runtime.background import BackgroundTaskManager, run_in_background
from myharness.runtime.scheduler import Scheduler, schedule_task
from myharness.runtime.persistence import TaskState, save_state, load_state

__all__ = [
    "BackgroundTaskManager",
    "run_in_background",
    "Scheduler",
    "schedule_task",
    "TaskState",
    "save_state",
    "load_state",
]
