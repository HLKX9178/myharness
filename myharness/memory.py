"""长期记忆：跨会话保存模型认为值得记住的事实。

参考 s09 教程重构：

  - 存储：`.memory/` 目录，每条记忆一个 `.md` 文件 + `MEMORY.md` 索引
  - 召回：关键词匹配检索 top-k 条，注入用户消息（Phase 2 可改为模型选择）
  - 提取：Phase 3 将改为 Stop hook 自动触发，Phase 1 暂时保留 remember 工具
  - 整理：达到 20 条后自动合并（Phase 4 实现）

记忆类型（s09）：

  - user: 用户的长期偏好
  - feedback: 以后仍适用的工作反馈
  - project: 稳定的项目事实
  - reference: 外部资料或查找线索
"""

import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable

RECALL_LIMIT = 3
MAX_MEMORY_CHARS = 500
CONSOLIDATION_THRESHOLD = 20

_ASCII_WORD = re.compile(r"[a-z0-9_]{2,}")
_CJK_RUN = re.compile(r"[一-鿿]+")
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|(api[_-]?key|secret|password|token)\s*[:=]\s*\S{8,})",
    re.IGNORECASE,
)

VALID_TYPES = {"user", "feedback", "project", "reference"}


def _tokens(text: str) -> set[str]:
    """提取文本中的关键词：ASCII 单词 + 中文二元组。"""
    lowered = text.lower()
    tokens = set(_ASCII_WORD.findall(lowered))
    for run in _CJK_RUN.findall(lowered):
        if len(run) == 1:
            tokens.add(run)
        tokens.update(run[i:i + 2] for i in range(len(run) - 1))
    return tokens


def memory_slug(name: str) -> str:
    """name -> 文件名前缀（kebab-case，ASCII-safe）。"""
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_]+", "-", slug).strip("-")
    return slug[:64] or "memory"


def memory_document(name: str, mem_type: str, description: str, body: str) -> str:
    """生成带 frontmatter 的 Markdown 文档。"""
    return f"---\nname: {name}\ndescription: {description}\ntype: {mem_type}\n---\n\n{body}\n"


def parse_memory_file(path: Path) -> dict[str, str]:
    """解析 .md 文件，返回 {name, description, type, body}；格式错误抛 ValueError。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"记忆文件 {path} 缺少 frontmatter")

    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError(f"记忆文件 {path} 的 frontmatter 没有结束") from None

    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"记忆文件 {path} frontmatter 格式错误：{line!r}")
        meta[key.strip()] = value.strip()

    name = meta.get("name", "")
    description = meta.get("description", "")
    mem_type = meta.get("type", "")
    body = "\n".join(lines[end + 1:]).strip()

    if not name or not description or not mem_type:
        raise ValueError(f"记忆文件 {path} 必须提供 name、description 和 type")
    if mem_type not in VALID_TYPES:
        raise ValueError(f"记忆文件 {path} 的 type 必须是 {VALID_TYPES} 之一，实际是 {mem_type!r}")
    if not body:
        raise ValueError(f"记忆文件 {path} 正文为空")
    return {"name": name, "description": description, "type": mem_type, "body": body}


def rebuild_memory_index(memory_dir: Path) -> None:
    """扫描 .md 文件，重新生成 MEMORY.md 索引。"""
    index_path = memory_dir / "MEMORY.md"
    lines = ["# Memory Index\n"]
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        try:
            mem = parse_memory_file(path)
            lines.append(f"- [{mem['name']}]({path.name}) — {mem['description']}\n")
        except ValueError:
            lines.append(f"- [⚠️ {path.name}](path.name) — (parse error)\n")
    index_path.write_text("".join(lines), encoding="utf-8")


class MemoryStore:
    """.memory/ 目录存储，每条记忆一个 .md 文件。"""

    def __init__(self, memory_dir: Path):
        self.memory_dir = memory_dir
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self._memories: list[dict[str, str]] = self._load()

    def _load(self) -> list[dict[str, str]]:
        """扫描 .md 文件加载全部记忆；损坏的文件跳过并在索引中标注。"""
        memories = []
        for path in sorted(self.memory_dir.glob("*.md")):
            if path.name == "MEMORY.md":
                continue
            try:
                mem = parse_memory_file(path)
                mem["_path"] = str(path)
                memories.append(mem)
            except ValueError as exc:
                # 不让一个坏文件阻止启动，但记录到索引里
                print(f"[warn] 跳过损坏的记忆文件 {path}: {exc}")
        if memories or any(self.memory_dir.glob("*.md")):
            rebuild_memory_index(self.memory_dir)
        return memories

    def __len__(self) -> int:
        return len(self._memories)

    def search(self, query: str, limit: int = RECALL_LIMIT) -> list[dict[str, str]]:
        """按词重叠打分，返回最多 limit 条；同分时新的优先。"""
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        scored = []
        for index, memory in enumerate(self._memories):
            text = f"{memory['name']} {memory['description']} {memory['body']}"
            score = len(query_tokens & _tokens(text))
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
        for mem in hits:
            lines.append(f"- [{mem['type']}] {mem['description']}")
        return "\n".join(lines)

    # ---- 工具：定义与实现相邻（契约 3）----

    def remember(self, content: str, mem_type: str = "user") -> str:
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
            if mem_type not in VALID_TYPES:
                return f"Error: type must be one of {VALID_TYPES}, got {mem_type!r}."

            # 检查重复
            for mem in self._memories:
                if mem["body"] == text:
                    return "Already remembered (identical memory exists)."

            # 生成 name 和 description（简化：取前 60 字符）
            name = text[:60]
            description = text[:100]

            # 写入 .md 文件
            slug = memory_slug(name)
            path = self.memory_dir / f"{slug}.md"
            counter = 1
            while path.exists():
                path = self.memory_dir / f"{slug}-{counter}.md"
                counter += 1

            doc = memory_document(name, mem_type, description, text)
            path.write_text(doc, encoding="utf-8")

            # 更新内存
            self._memories.append({
                "name": name,
                "description": description,
                "type": mem_type,
                "body": text,
                "_path": str(path),
            })

            # 重建索引
            rebuild_memory_index(self.memory_dir)

            return f"Remembered. Store now holds {len(self._memories)} memories."
        except OSError as exc:
            return f"Error: failed to write memory file: {exc}"

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
                    "type": {
                        "type": "string",
                        "enum": list(VALID_TYPES),
                        "description": (
                            "user: long-term preference | feedback: reusable work guidance | "
                            "project: stable project fact | reference: external resource pointer"
                        ),
                    },
                },
                "required": ["content"],
            },
        }]

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {"remember": self.remember}
