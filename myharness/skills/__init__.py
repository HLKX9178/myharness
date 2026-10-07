"""技能：按需加载的能力说明。

两段式加载，保证 prompt 不随技能数量线性膨胀（M3 验收）：

  1. system prompt 只放目录：每个技能一行「名字: 描述」
  2. 模型判断任务匹配时调用 load_skill(name)，全文以 tool_result 进入上下文

技能文件是带 frontmatter 的 Markdown：

    ---
    name: code_review
    description: 一句话说明何时使用
    ---
    正文……

load_skill 只按名字查目录，从不拼接模型给的路径，因此不存在路径穿越。
"""

from pathlib import Path
from typing import Callable

BUILTIN_SKILL_DIR = Path(__file__).resolve().parent

MAX_DESCRIPTION_CHARS = 200


def _parse_skill_file(path: Path) -> tuple[str, str, str]:
    """解析技能文件，返回 (name, description, body)。格式错误抛 ValueError。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"技能文件 {path} 缺少 frontmatter：第一行必须是 ---")

    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration:
        raise ValueError(f"技能文件 {path} 的 frontmatter 没有结束的 ---") from None

    meta: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"技能文件 {path} 的 frontmatter 行格式应为 key: value，实际是 {line!r}")
        meta[key.strip()] = value.strip()

    name = meta.get("name", "")
    description = meta.get("description", "")
    body = "\n".join(lines[end + 1:]).strip()
    if not name or not description:
        raise ValueError(f"技能文件 {path} 必须同时提供 name 和 description")
    if len(description) > MAX_DESCRIPTION_CHARS:
        raise ValueError(
            f"技能 {name} 的 description 超过 {MAX_DESCRIPTION_CHARS} 字符；"
            f"目录会进入每次请求，细节请写进正文"
        )
    if not body:
        raise ValueError(f"技能文件 {path} 正文为空")
    return name, description, body


class SkillLibrary:
    """技能目录。启动时扫描一次，之后只读。"""

    def __init__(self, skill_dirs: list[Path]):
        self._skills: dict[str, tuple[str, str]] = {}
        for skill_dir in skill_dirs:
            for path in sorted(skill_dir.glob("*.md")):
                name, description, body = _parse_skill_file(path)
                if name in self._skills:
                    raise ValueError(f"技能名 {name!r} 重复定义（{path}）")
                self._skills[name] = (description, body)

    def names(self) -> list[str]:
        return list(self._skills)

    def catalog(self) -> str:
        """拼进 system prompt 的技能目录。没有技能时返回空串。"""
        if not self._skills:
            return ""
        lines = [
            "## Skills",
            "The skills below can be loaded on demand. When a task matches one, "
            "call load_skill(name) first to get its full instructions.",
        ]
        lines += [f"- {name}: {desc}" for name, (desc, _) in self._skills.items()]
        return "\n".join(lines)

    # ---- 工具：定义与实现相邻（契约 3）----

    def load_skill(self, name: str) -> str:
        """返回技能全文；未知技能返回 Error 字符串。"""
        skill = self._skills.get(name)
        if skill is None:
            available = ", ".join(self._skills) or "(none)"
            return f"Error: unknown skill {name!r}. Available skills: {available}"
        return f"# Skill: {name}\n\n{skill[1]}"

    def tool_defs(self) -> list[dict]:
        if not self._skills:
            return []
        return [{
            "name": "load_skill",
            "description": (
                "Load the full instructions of a skill listed in the system prompt. "
                "Call this before working on a task that matches the skill."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": self.names(),
                        "description": "Skill name from the catalog",
                    },
                },
                "required": ["name"],
            },
        }]

    def handlers(self) -> dict[str, Callable[..., str]]:
        if not self._skills:
            return {}
        return {"load_skill": self.load_skill}
