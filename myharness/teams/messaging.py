"""MessageQueue：Agent 间消息传递。

核心功能：
1. 消息发送与持久化
2. 消息接收（FIFO 队列）
3. 投递确认（ack）机制
4. 未确认消息的重新投递

设计：
- 每个 Agent 有独立的收件箱（inbox）
- 消息持久化为 JSON Lines
- 消息状态：PENDING（待投递）-> DELIVERED（已投递）-> ACKED（已确认）
"""

import json
import threading
from dataclasses import dataclass, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any


class MessageStatus(Enum):
    """消息状态。"""
    PENDING = "pending"        # 待投递
    DELIVERED = "delivered"    # 已投递
    ACKED = "acked"           # 已确认


@dataclass
class Message:
    """消息数据结构。"""
    message_id: str
    from_agent: str
    to_agent: str
    content: Any
    status: MessageStatus
    created_at: str
    delivered_at: str | None = None
    acked_at: str | None = None


class MessageQueue:
    """
    Agent 间消息队列。

    每个 Agent 有独立的收件箱文件：{queue_dir}/{agent_id}.jsonl
    """

    def __init__(self, queue_dir: Path):
        """
        初始化消息队列。

        Args:
            queue_dir: 队列目录
        """
        self.queue_dir = queue_dir
        self._lock = threading.Lock()
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        """确保目录存在。"""
        self.queue_dir.mkdir(parents=True, exist_ok=True)

    def send(self, from_agent: str, to_agent: str, content: Any) -> str:
        """
        发送消息。

        Args:
            from_agent: 发送者 Agent ID
            to_agent: 接收者 Agent ID
            content: 消息内容（可序列化为 JSON）

        Returns:
            message_id: 消息 ID
        """
        import uuid
        message_id = f"msg_{uuid.uuid4().hex[:12]}"

        message = Message(
            message_id=message_id,
            from_agent=from_agent,
            to_agent=to_agent,
            content=content,
            status=MessageStatus.PENDING,
            created_at=datetime.now().isoformat(),
        )

        with self._lock:
            self._append_message(to_agent, message)

        return message_id

    def receive(self, agent_id: str, limit: int = 10) -> list[Message]:
        """
        接收消息（PENDING 状态）。

        接收后消息状态变为 DELIVERED，需要调用 ack() 确认。

        Args:
            agent_id: Agent ID
            limit: 最多接收消息数

        Returns:
            消息列表（FIFO 顺序）
        """
        with self._lock:
            messages = self._load_messages(agent_id)
            pending = [
                msg for msg in messages
                if msg.status == MessageStatus.PENDING
            ]
            pending = sorted(pending, key=lambda m: m.created_at)[:limit]

            # 标记为 DELIVERED
            for msg in pending:
                msg.status = MessageStatus.DELIVERED
                msg.delivered_at = datetime.now().isoformat()
                self._append_message(agent_id, msg)

            return pending

    def ack(self, agent_id: str, message_id: str) -> bool:
        """
        确认消息已处理。

        Args:
            agent_id: Agent ID
            message_id: 消息 ID

        Returns:
            是否成功确认
        """
        with self._lock:
            messages = self._load_messages(agent_id)
            msg = next(
                (m for m in messages if m.message_id == message_id),
                None
            )

            if not msg:
                return False
            if msg.to_agent != agent_id:
                return False
            if msg.status != MessageStatus.DELIVERED:
                return False

            msg.status = MessageStatus.ACKED
            msg.acked_at = datetime.now().isoformat()
            self._append_message(agent_id, msg)
            return True

    def list_messages(
        self,
        agent_id: str,
        status: MessageStatus | None = None,
    ) -> list[Message]:
        """
        列出消息。

        Args:
            agent_id: Agent ID
            status: 过滤状态（None 表示全部）

        Returns:
            消息列表（按创建时间顺序）
        """
        with self._lock:
            messages = self._load_messages(agent_id)
            if status:
                messages = [m for m in messages if m.status == status]
            return sorted(messages, key=lambda m: m.created_at)

    def _load_messages(self, agent_id: str) -> list[Message]:
        """
        加载 Agent 的收件箱（最新状态）。

        Args:
            agent_id: Agent ID

        Returns:
            消息列表
        """
        inbox_file = self.queue_dir / f"{agent_id}.jsonl"
        if not inbox_file.exists():
            return []

        # {message_id: Message} 映射（保留最新状态）
        messages_dict: dict[str, Message] = {}

        try:
            with inbox_file.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    data["status"] = MessageStatus(data["status"])
                    msg = Message(**data)
                    messages_dict[msg.message_id] = msg
        except Exception as exc:
            print(f"[msgqueue] Failed to load messages for {agent_id}: {exc}")

        return list(messages_dict.values())

    def _append_message(self, agent_id: str, message: Message) -> None:
        """
        追加消息到收件箱（append-only）。

        Args:
            agent_id: Agent ID
            message: 消息对象
        """
        inbox_file = self.queue_dir / f"{agent_id}.jsonl"
        data = asdict(message)
        data["status"] = message.status.value

        with inbox_file.open("a", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.write("\n")
