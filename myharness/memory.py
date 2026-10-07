"""长期记忆：跨会话保存模型认为值得记住的事实。

职责划分：

  - 写入：由模型决定。模型调用 remember(content)，Harness 只负责校验与落盘。
  - 读取：由 Harness 决定。recall_hook 挂在 UserPromptSubmit 上，按关键词
    检索 top-k 条注入本轮用户消息。

注入量有硬上限（RECALL_LIMIT 条 × MAX_MEMORY_CHARS 字符），
与记忆库大小、会话长度都无关（M3 验收）。

检索是确定性的词重叠打分：ASCII 单词 + 中文字符二元组。不引入向量库依赖；
代价是同义词召回不到，这是已知边界。
"""

import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

RECALL_LIMIT = 3
MAX_MEMORY_CHARS = 500

_ASCII_WORD = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN = re.compile(r"[一-鿿]+")
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(api[_-]?key|secret|password|token)\s*[:=]\s*\S{8,})",
    re.IGNORECASE,
)


def _tokens(text: str) -> set[str]:
    lowered = text.lower()
    tokens = set(_ASCII_WORD.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


class MemoryStore:
    """JSON 文件存储。每次写入整体原子替换，进程中途被杀也不会留下半个文件。"""

    def __init__(self, path: Path):
        self.path = path
        self._memories: list[dict[str, str]] = self._load()

    def _load(self) -> list[dict[str, str]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"记忆文件 {self.path} 已损坏（{exc}）；请修复或移走该文件后重启"
            ) from exc
        return list(data.get("memories", []))

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"memories": self._memories}, ensure_ascii=False, indent=2)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
            os.replace(tmp, self.path)
        except OSError:
            Path(tmp).unlink(missing_ok=True)
            raise

    def __len__(self) -> int:
        return len(self._memories)

    def search(self, query: str, limit: int = RECALL_LIMIT) -> list[dict[str, str]]:
        """按词重叠打分，返回最多 limit 条；同分时新的优先。"""
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        scored = []
        for index, memory in enumerate(self._memories):
            score = len(query_tokens & _tokens(memory["content"]))
            if score:
                scored.append((score, index, memory))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [memory for _, _, memory in scored[:limit]]

    # ---- Hook：UserPromptSubmit ----

    def recall_hook(self, query: str) -> str | None:
        hits = self.search(query)
        if not hits:
            return None
        lines = ["Relevant long-term memories (may be outdated; verify before relying on them):"]
        lines += [f"- [{m['created_at'][:10]}] {m['content']}" for m in hits]
        return "\n".join(lines)

    # ---- 工具：定义与实现相邻（契约 3）----

    def remember(self, content: str) -> str:
        """保存一条记忆；校验失败返回 Error 字符串。"""
        try:
            text = " ".join(str(content).split())
            if not text:
                return "Error: content is empty. Provide one self-contained fact to remember."
            if len(text) > MAX_MEMORY_CHARS:
                return (
                    f"Error: content is {len(text)} chars, limit is {MAX_MEMORY_CHARS}. "
                    f"Condense it to the single fact worth keeping."
                )
            if _SECRET.search(text):
                return "Error: content looks like a credential. Secrets must not be stored in memory."
            if any(m["content"] == text for m in self._memories):
                return "Already remembered (identical memory exists)."

            self._memories.append({
                "id": f"mem_{uuid.uuid4().hex[:8]}",
                "content": text,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            })
            self._save()
            return f"Remembered. Store now holds {len(self._memories)} memories."
        except OSError as exc:
            return f"Error: failed to write memory file {self.path}: {exc}"

    def tool_defs(self) -> list[dict]:
        return [{
            "name": "remember",
            "description": (
                "Save one durable fact to long-term memory so future sessions can recall it: "
                "user preferences, project conventions, decisions and their reasons, "
                "non-obvious pitfalls. Do not save things derivable from the code, "
                "transient task state, or secrets. One self-contained fact per call. "
                "Recall is keyword-based: write the fact in the user's language and "
                "keep the exact key terms the user used, or it will not be found later."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": f"The fact, max {MAX_MEMORY_CHARS} chars",
                    },
                },
                "required": ["content"],
            },
        }]

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {"remember": self.remember}
