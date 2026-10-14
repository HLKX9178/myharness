"""TeamAgent：多 Agent 协作的 Agent 封装。

核心功能：
1. 独立的工作目录（隔离文件操作）
2. 独立的上下文（messages 列表）
3. 任务认领与执行
4. 消息收发

设计：
- 每个 TeamAgent 包装现有的 agent_loop
- 工作目录：{team_dir}/agent_{agent_id}/workspace/
- 会话目录：{team_dir}/agent_{agent_id}/sessions/
- workspace 通过显式参数传递，不再使用全局状态
"""

from pathlib import Path
from typing import Any

from myharness.config import (
    MAX_TOKENS,
    build_system_prompt,
    provider,
)
from myharness.loop import agent_loop
from myharness.session import Session
from myharness.teams.taskboard import TaskBoard, TaskStatus
from myharness.teams.messaging import MessageQueue


class TeamAgent:
    """
    团队 Agent 封装。

    每个 Agent 有：
    - 独立的工作目录（文件隔离）
    - 独立的上下文（messages 隔离）
    - 任务认领与执行能力
    - 消息收发能力
    """

    def __init__(
        self,
        agent_id: str,
        team_dir: Path,
        taskboard: TaskBoard,
        message_queue: MessageQueue,
    ):
        """
        初始化 TeamAgent。

        Args:
            agent_id: Agent ID（唯一标识）
            team_dir: 团队目录
            taskboard: 共享任务板
            message_queue: 消息队列
        """
        self.agent_id = agent_id
        self.team_dir = team_dir
        self.taskboard = taskboard
        self.message_queue = message_queue

        # 独立的工作目录
        self.agent_dir = team_dir / f"agent_{agent_id}"
        self.workspace = self.agent_dir / "workspace"
        self.session_dir = self.agent_dir / "sessions"

        self._ensure_dirs()

        # 会话管理
        self.session = Session(self.session_dir)
        self.messages: list = self.session.messages

    def _ensure_dirs(self) -> None:
        """确保目录存在。"""
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def claim_and_execute(self) -> dict[str, Any]:
        """
        认领并执行一个任务。

        工作流：
        1. 从任务板认领任务
        2. 准备上下文
        3. 在独立工作目录执行 agent_loop（通过显式参数传递 workspace）
        4. 标记任务完成/失败

        Returns:
            执行结果：{"success": bool, "task_id": str, "result": Any}
        """
        # 1. 认领任务
        task = self.taskboard.claim_task(self.agent_id)
        if not task:
            return {"success": False, "message": "No tasks available"}

        # 2. 准备上下文
        self.messages.clear()
        self.messages.append({
            "role": "user",
            "content": f"Task: {task.title}\n\n{task.description}"
        })

        # 3. 在独立 workspace 执行（通过显式参数传递）
        try:
            agent_loop(self.messages, workspace=self.workspace)

            # 4. 提取结果（最后一条 assistant 消息）
            result = None
            for msg in reversed(self.messages):
                if msg.get("role") == "assistant":
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if getattr(block, "type", None) == "text":
                                result = block.text
                                break
                    elif isinstance(content, str):
                        result = content
                    break

            # 5. 标记任务完成
            self.taskboard.complete_task(task.task_id, self.agent_id, result)
            self.session.save()

            return {
                "success": True,
                "task_id": task.task_id,
                "result": result,
            }

        except Exception as exc:
            # 标记任务失败
            error_msg = f"{type(exc).__name__}: {exc}"
            self.taskboard.fail_task(task.task_id, self.agent_id, error_msg)
            self.session.save()

            return {
                "success": False,
                "task_id": task.task_id,
                "error": error_msg,
            }

    def send_message(self, to_agent: str, content: Any) -> str:
        """
        发送消息给其他 Agent。

        Args:
            to_agent: 目标 Agent ID
            content: 消息内容

        Returns:
            message_id: 消息 ID
        """
        return self.message_queue.send(self.agent_id, to_agent, content)

    def receive_messages(self, limit: int = 10) -> list:
        """
        接收消息。

        Args:
            limit: 最多接收消息数

        Returns:
            消息列表
        """
        messages = self.message_queue.receive(self.agent_id, limit)

        # 自动确认（简化版）
        for msg in messages:
            self.message_queue.ack(self.agent_id, msg.message_id)

        return messages

    def get_status(self) -> dict[str, Any]:
        """
        获取 Agent 状态。

        Returns:
            状态字典
        """
        return {
            "agent_id": self.agent_id,
            "workspace": str(self.workspace),
            "session_messages": len(self.messages),
        }
