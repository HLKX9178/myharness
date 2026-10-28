"""会话持久化：保存和恢复对话历史。

支持：
1. 进程重启后继续对话（M2 验收标准）
2. 会话快照保存为 JSON 格式
3. 按会话 ID 管理多个独立会话
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def _preview(messages: list[dict[str, Any]], limit: int = 60) -> str:
    """取第一条用户消息的前若干字符，用于在会话列表里辨认。

    列表只给 ID 和时间戳等于没给：同一个问题的多次重问、恢复过的旧会话，
    肉眼分不出来。
    """
    for message in messages:
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = " ".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            text = ""
        text = " ".join(text.split())
        if text:
            return text[:limit]
    return ""


class Session:
    """会话管理：持久化对话历史。"""

    def __init__(self, session_dir: Path, session_id: str | None = None):
        """
        初始化会话。

        Args:
            session_dir: 会话数据存储目录
            session_id: 会话 ID（None 则创建新会话）
        """
        self.session_dir = session_dir
        self.session_id = session_id or self._generate_session_id()
        self.session_file = self.session_dir / f"{self.session_id}.json"
        self.messages: list[dict[str, Any]] = []
        self.metadata: dict[str, Any] = {
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
        }

    def _generate_session_id(self) -> str:
        """生成新的会话 ID（格式：session_YYYYMMDD_HHMMSS）。"""
        return f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    def add_message(self, role: str, content: Any) -> None:
        """
        添加一条消息到会话历史。

        Args:
            role: 消息角色（user/assistant）
            content: 消息内容（可以是文本或工具调用）
        """
        self.messages.append({"role": role, "content": content})
        self.metadata["updated_at"] = datetime.now().isoformat()

    def save(self) -> None:
        """保存会话到磁盘。"""
        # 序列化消息：将 Anthropic 对象转换为字典
        serializable_messages = []
        for msg in self.messages:
            serialized_msg = {"role": msg["role"], "content": []}

            # 处理 content（可能是字符串或对象列表）
            if isinstance(msg["content"], str):
                serialized_msg["content"] = msg["content"]
            elif isinstance(msg["content"], list):
                for item in msg["content"]:
                    # Anthropic SDK 对象需要转换
                    if hasattr(item, "model_dump"):
                        serialized_msg["content"].append(item.model_dump())
                    elif isinstance(item, dict):
                        serialized_msg["content"].append(item)
                    else:
                        # 其他类型尝试转字典
                        serialized_msg["content"].append(str(item))
            else:
                serialized_msg["content"] = msg["content"]

            serializable_messages.append(serialized_msg)

        data = {
            "session_id": self.session_id,
            "metadata": self.metadata,
            "messages": serializable_messages,
        }

        with self.session_file.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, session_dir: Path, session_id: str) -> "Session":
        """
        从磁盘加载会话。

        Args:
            session_dir: 会话数据目录
            session_id: 会话 ID

        Returns:
            恢复的 Session 对象

        Raises:
            FileNotFoundError: 会话不存在
        """
        session_file = session_dir / f"{session_id}.json"
        if not session_file.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")

        with session_file.open("r", encoding="utf-8") as f:
            data = json.load(f)

        session = cls(session_dir, session_id)
        session.messages = data["messages"]
        session.metadata = data["metadata"]
        return session

    @classmethod
    def list_sessions(cls, session_dir: Path) -> list[dict[str, Any]]:
        """
        列出所有会话。

        Returns:
            会话列表，按更新时间倒序：
            [{"id", "created_at", "updated_at", "message_count", "preview"}]

            preview 取第一条用户消息的前 60 个字符 —— 只有 ID 和时间戳的话，
            38 个存档摆在面前根本认不出哪条是想要的。
        """
        sessions = []
        for file in session_dir.glob("session_*.json"):
            try:
                with file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                messages = data.get("messages") or []
                sessions.append({
                    "id": data["session_id"],
                    "created_at": data["metadata"]["created_at"],
                    "updated_at": data["metadata"]["updated_at"],
                    "message_count": len(messages),
                    "preview": _preview(messages),
                })
            except Exception:
                continue  # 跳过损坏的会话文件

        # 按更新时间倒序排列
        sessions.sort(key=lambda s: s["updated_at"], reverse=True)
        return sessions
