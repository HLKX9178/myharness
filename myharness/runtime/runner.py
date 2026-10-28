"""执行到期的调度任务。

一条调度任务 = 一轮完整的 agent 对话。每个任务有自己的会话目录
（``.myharness/scheduled/<task_id>/``），跑完落盘，可以像普通会话一样用
``python -m myharness --resume <session_id>`` 打开回看。

**为什么是串行**：``agent_loop`` 依赖若干进程级状态（guards 的重复调用计数、
hook 的跨轮状态、工具层的全局单例）。两轮并发会互相污染，表现出来是莫名其妙
的「重复调用被拦截」或 hook 状态错位。多 Agent 场景没有这个问题 —— 它们各自
跑在独立进程里；调度器只有一个进程，所以用一把锁把并发挡在外面。
"""

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:  # 仅为类型标注，避免运行期循环导入
    from myharness.runtime.scheduler import ScheduledTask


@dataclass
class JobResult:
    """一次调度的执行结果。"""

    session_id: str
    output: str


# 同一进程内串行执行，理由见模块 docstring
_EXECUTION_LOCK = threading.Lock()


def execute_task(task: "ScheduledTask") -> JobResult:
    """
    执行一条调度任务。

    Args:
        task: 调度任务

    Returns:
        JobResult（会话 ID 与最后一轮文本回复）
    """
    with _EXECUTION_LOCK:
        return _run(task)


def _run(task: "ScheduledTask") -> JobResult:
    # 延迟导入：runtime 是纯库，导入期不该把 config（含凭据校验）和整个
    # loop 一起拉进来
    from myharness.config import SCHEDULE_DIR
    from myharness.loop import agent_loop
    from myharness.session import Session

    workspace = Path(task.workspace).resolve() if task.workspace else None
    if workspace is not None and not workspace.is_dir():
        # 目录被删了就明确失败：否则每一轮都是「Path escapes workspace」反复重试
        raise RuntimeError(f"Scheduled workspace no longer exists: {workspace}")

    session_dir = SCHEDULE_DIR / task.task_id
    session_dir.mkdir(parents=True, exist_ok=True)

    session = Session(session_dir)
    session.add_message("user", task.prompt)

    # 每轮都是新会话：cron 任务大多彼此独立，累积历史只会把上下文越拖越贵
    outcome = agent_loop(session.messages, workspace=workspace)
    session.save()

    # agent_loop 对 provider 失败、预算耗尽、无进展都是**优雅结束**而不是抛异常
    # （这样 CLI 不会崩），所以这里必须自己判断「这一轮到底有没有跑成」。
    # LoopOutcome 就是为此存在的判据：只有 completed 算成功。
    #
    # 早先这里是「有没有产生 assistant 消息」的启发式 —— 它能抓住 provider
    # 挂掉，但抓不住「跑了 200 轮还在原地打转却说过几句话」：那种任务会被
    # 记成 ok，定时任务静默空转而 list_schedules 显示一切正常。
    if not outcome.completed:
        raise RuntimeError(
            f"agent did not complete ({outcome.reason}): "
            f"{outcome.detail or 'no detail'}; session={session.session_id}"
        )

    return JobResult(
        session_id=session.session_id,
        output=_last_text(session.messages),
    )


def _last_text(messages: list[dict[str, Any]]) -> str:
    """取最后一条 assistant 消息里的文本（模型可能只调工具、不产出文本）。"""
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = "\n".join(t for t in texts if t)
            if joined:
                return joined
    return ""
