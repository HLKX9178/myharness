"""Teams 模块：多 Agent 协作。

提供：
1. TaskBoard - 共享任务队列
2. MessageQueue - Agent 间消息传递
3. FileLock - 并发文件修改保护
4. TeamAgent - Agent 封装与隔离
5. AgentManager - Agent 进程管理器
6. AgentWorker - Agent 工作循环
"""

from myharness.teams.taskboard import TaskBoard, Task, TaskStatus
from myharness.teams.messaging import MessageQueue, Message, MessageStatus
from myharness.teams.filelock import FileLock
from myharness.teams.agent import TeamAgent
from myharness.teams.manager import AgentManager
from myharness.teams.worker import AgentWorker, run_worker

__all__ = [
    "TaskBoard",
    "Task",
    "TaskStatus",
    "MessageQueue",
    "Message",
    "MessageStatus",
    "FileLock",
    "TeamAgent",
    "AgentManager",
    "AgentWorker",
    "run_worker",
]
