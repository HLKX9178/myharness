"""迁移脚本：将旧的 memories.json 转为 .md 文件格式。

用法：python -m myharness.migrate_memory
"""

import json
import sys
from pathlib import Path

from myharness.memory import MemoryStore, memory_document, memory_slug, rebuild_memory_index


def migrate(json_path: Path, memory_dir: Path) -> None:
    """将 JSON 格式的记忆迁移到 .md 文件。"""
    if not json_path.exists():
        print(f"[info] {json_path} 不存在，无需迁移")
        return

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[error] {json_path} 解析失败: {exc}")
        sys.exit(1)

    memories = data.get("memories", [])
    if not memories:
        print(f"[info] {json_path} 为空，无需迁移")
        return

    memory_dir.mkdir(parents=True, exist_ok=True)
    migrated = 0

    for mem in memories:
        content = mem.get("content", "")
        if not content:
            continue

        # 旧格式没有 type 字段，默认 user
        mem_type = "user"
        name = content[:60]
        description = content[:100]

        slug = memory_slug(name)
        path = memory_dir / f"{slug}.md"
        counter = 1
        while path.exists():
            path = memory_dir / f"{slug}-{counter}.md"
            counter += 1

        doc = memory_document(name, mem_type, description, content)
        path.write_text(doc, encoding="utf-8")
        migrated += 1

    rebuild_memory_index(memory_dir)
    print(f"[success] 迁移 {migrated} 条记忆到 {memory_dir}")
    print(f"[info] 旧文件 {json_path} 已保留，可手动删除")


if __name__ == "__main__":
    from myharness.config import REPO_ROOT

    json_path = REPO_ROOT / ".myharness" / "memory" / "memories.json"
    memory_dir = REPO_ROOT / ".myharness" / "memory"

    migrate(json_path, memory_dir)
