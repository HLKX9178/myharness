"""TeamAgent：多 Agent 协作的 Agent 封装。

核心功能：
1. 独立的工作目录（隔离文件操作）
2. 独立的上下文（messages 列表）
3. 任务认领与执行
4. 消息收发

设计：
- 每个 TeamAgent 包装现有的 agent_loop
- 工作目录：{team_dir}/{agent_id}/workspace/
- 会话目录：{team_dir}/{agent_id}/sessions/
- workspace 通过显式参数传递，不再使用全局状态
"""

from pathlib import Path
from typing import Any

from myharness.hooks import Block, submit_user_prompt
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

        # 独立的工作目录。
        # agent_id 本身已是完整标识（AgentManager 传的是 "agent_0"），
        # 这里再拼一次 "agent_" 会得到 agent_agent_0 这种双层前缀。
        self.agent_dir = team_dir / agent_id
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
        #
        # 走与交互式会话同一条记忆注入路径（UserPromptSubmit）。worker 之前是
        # 靠 system prompt 里的召回拿到记忆的，那条路因为前缀缓存已经移除；
        # 注入只发生在**新建的这条消息**上，不会作废任何已有前缀。
        task_prompt = f"Task: {task.title}\n\n{task.description}"
        submitted = submit_user_prompt(task_prompt)
        if isinstance(submitted, Block):
            # worker 没有交互界面，输入被 hook 拒绝时只能让任务失败，
            # 不能假装什么都没发生继续执行。
            reason = f"prompt rejected by hook: {submitted.reason}"
            self.taskboard.fail_task(task.task_id, self.agent_id, reason)
            self.session.save()
            return {"success": False, "task_id": task.task_id, "error": reason}

        self.messages.clear()
        self.messages.append({"role": "user", "content": submitted})

        # 3. 在独立 workspace 执行（通过显式参数传递）
        try:
            outcome = agent_loop(self.messages, workspace=self.workspace)

            # 4. 提取结果（最后一条 assistant 消息）
            result = self._extract_last_reply(self.messages)

            # 4.5 没走完就不是成果：预算耗尽、轮次用尽、无进展、连续失败都
            # 会留下「说过几句话」的历史，只看有没有消息会把它们当成功。
            # 交给任务板记失败，理由写清楚，免得下游以为工作已交付。
            if not outcome.completed:
                reason = (
                    f"loop ended without completing ({outcome.reason}): "
                    f"{outcome.detail or 'no detail'}"
                )
                marked = self.taskboard.fail_task(task.task_id, self.agent_id, reason)
                self.session.save()
                return {
                    "success": False,
                    "task_id": task.task_id,
                    "error": reason if marked else "claim lost: task was reclaimed",
                }

            # 5. 标记任务完成
            # 返回 False 说明认领已经失效：执行时间超过 timeout_seconds，
            # 任务被别的 worker 当作「崩溃遗留」接管了。本次副作用已经
            # 落在 workspace 里，无法回收，但结果不能再写进任务板 ——
            # 否则会用迟到的结果覆盖接管者的状态。
            completed = self.taskboard.complete_task(
                task.task_id, self.agent_id, result
            )
            self.session.save()

            if not completed:
                return {
                    "success": False,
                    "task_id": task.task_id,
                    "error": "claim lost: task was reclaimed by another worker",
                }

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

    @staticmethod
    def _extract_last_reply(messages: list) -> str | None:
        """取最后一条 assistant 消息的文本内容。

        agent_loop 在写回 messages 之前会把 content block 归一成 dict
        （`block.model_dump()`），所以这里必须按 dict 解析。早期实现用
        `getattr(block, "type")` 读属性，对 dict 永远取不到值 —— 表现为
        任务正常完成，但 result 恒为 None。
        """
        for msg in reversed(messages):
            if msg.get("role") != "assistant":
                continue

            content = msg.get("content", [])
            if isinstance(content, str):
                return content or None

            texts = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
                elif getattr(block, "type", None) == "text":
                    texts.append(getattr(block, "text", ""))
            return "\n".join(texts) if texts else None

        return None

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
