"""长期记忆：跨会话保存模型认为值得记住的事实。

参考 s09 教程重构：

  - 存储：`.memory/` 目录，每条记忆一个 `.md` 文件 + `MEMORY.md` 索引
  - 召回：关键词匹配检索 top-k 条，注入用户消息（Phase 2 可改为模型选择）
  - 提取：Stop hook 自动触发，模型返回候选，Harness 校验后写入（Phase 3）
  - 整理：达到 20 条后自动合并（Phase 4 实现）

记忆类型（s09）：

  - user: 用户的长期偏好
  - feedback: 以后仍适用的工作反馈
  - project: 稳定的项目事实
  - reference: 外部资料或查找线索
"""

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

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
_TRANSIENT = re.compile(
    r"(本次|这次|当前|此次|现在|今天|this session|current task|this time|right now|today)",
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

    # ---- Hook：Stop（Phase 3 自动提取）----

    def should_store_memory(self, candidate: dict[str, Any]) -> tuple[bool, str]:
        """校验候选记忆；返回 (是否通过, 拒绝原因)。"""
        content = candidate.get("content", "")
        mem_type = candidate.get("type", "")
        scope = candidate.get("scope", "")

        # 字段完整性
        if not content or not mem_type or not scope:
            return False, "missing required fields (content, type, scope)"
        if mem_type not in VALID_TYPES:
            return False, f"invalid type {mem_type!r}"
        if scope not in {"persistent", "current_task"}:
            return False, f"invalid scope {scope!r}"

        # scope 检查
        if scope == "current_task":
            return False, "scope is current_task, not persistent"

        # 内容校验
        text = " ".join(str(content).split())
        if not text:
            return False, "content is empty"
        if len(text) > MAX_MEMORY_CHARS:
            return False, f"content exceeds {MAX_MEMORY_CHARS} chars"
        if _SECRET.search(text):
            return False, "content looks like a credential"
        if _TRANSIENT.search(text):
            return False, "content contains transient expressions"

        # 重复检查：完全相同
        for mem in self._memories:
            if mem["body"] == text:
                return False, "identical memory already exists"

        # 高度相似检查（>80% 词重叠）
        candidate_tokens = _tokens(text)
        if candidate_tokens:
            for mem in self._memories:
                existing_tokens = _tokens(mem["body"])
                if not existing_tokens:
                    continue
                overlap = len(candidate_tokens & existing_tokens)
                similarity = overlap / min(len(candidate_tokens), len(existing_tokens))
                if similarity > 0.8:
                    return False, f"too similar to existing memory {mem['name'][:30]}"

        return True, ""

    def extract_memories_hook(self, messages: list, provider: Any = None) -> None:
        """Stop hook：提取本轮对话中值得保存的记忆。

        provider 由 bootstrap 传入，避免循环导入 config.provider。
        """
        if provider is None:
            return  # 测试时可能没有传 provider

        # 只看最近的对话（最多 10 条消息或最近 5 轮）
        recent = messages[-10:]
        if len(recent) < 2:
            return  # 对话太短，没什么可提取

        # 构造提取 prompt
        conversation = []
        for msg in recent:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if isinstance(content, str):
                conversation.append(f"{role}: {content[:200]}")
            elif isinstance(content, list):
                text_blocks = [b.text for b in content if hasattr(b, "text")]
                if text_blocks:
                    conversation.append(f"{role}: {text_blocks[0][:200]}")

        extraction_prompt = f"""Extract durable facts from this conversation that should be remembered for future sessions.

Conversation:
{chr(10).join(conversation)}

Return a JSON array of memory candidates. Each must have:
- content: one self-contained fact (max {MAX_MEMORY_CHARS} chars)
- type: user | feedback | project | reference
- scope: persistent | current_task

Only extract facts that apply to FUTURE sessions (scope: persistent). Do NOT extract:
- Transient commands ("this time", "for now", "in this session")
- Things derivable from code
- Temporary paths or one-time tasks

Return [] if nothing is worth remembering. Return ONLY valid JSON, no other text."""

        try:
            response = provider.chat(
                messages=[{"role": "user", "content": extraction_prompt}],
                max_tokens=1000,
            )
            text = "".join(
                b.text for b in response.content if hasattr(b, "text")
            ).strip()

            # 提取 JSON（可能被包在 ```json ... ``` 里）
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            candidates = json.loads(text)
            if not isinstance(candidates, list):
                return

            stored = 0
            for candidate in candidates:
                ok, reason = self.should_store_memory(candidate)
                if not ok:
                    continue

                # 写入记忆
                content = candidate["content"]
                mem_type = candidate["type"]
                name = content[:60]
                description = content[:100]

                slug = memory_slug(name)
                path = self.memory_dir / f"{slug}.md"
                counter = 1
                while path.exists():
                    path = self.memory_dir / f"{slug}-{counter}.md"
                    counter += 1

                doc = memory_document(name, mem_type, description, content)
                path.write_text(doc, encoding="utf-8")

                self._memories.append({
                    "name": name,
                    "description": description,
                    "type": mem_type,
                    "body": content,
                    "_path": str(path),
                })
                stored += 1

            if stored > 0:
                rebuild_memory_index(self.memory_dir)
                print(f"[memory] 自动提取并保存 {stored} 条记忆")

        except (json.JSONDecodeError, OSError, Exception) as exc:
            # 提取失败不影响主循环，静默忽略
            pass

    # ---- 工具：Phase 3 后移除，改为自动提取 ----

    def tool_defs(self) -> list[dict]:
        # Phase 3：移除 remember 工具，改为 Stop hook 自动提取
        return []

    def handlers(self) -> dict[str, Callable[..., str]]:
        return {}
