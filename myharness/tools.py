"""工具集。

每个工具由两部分组成，**必须成对出现**，少一个都不行（见 AGENTS.md 契约 3）：

  1. 实现 —— 真正执行的函数
  2. 定义 —— 放进 TOOLS，给模型看的 JSON Schema

职责边界：**工具只做业务**（读文件、跑命令），不做权限判定、不写审计日志。
那些属于 hook 层（见 AGENTS.md 契约 6）。

契约（见 AGENTS.md 契约 1）：所有工具函数只返回字符串，
出错返回 "Error: ..."，绝不抛异常。
"""

import glob
import subprocess
from pathlib import Path
from typing import Callable

from myharness.config import WORKDIR, skill_library


# ============================================================
# 实现
# ============================================================

def safe_path(p: str) -> Path:
    """把相对路径 p 转成绝对路径，并确保它落在工作目录内。

    这是**兜底防线**，不是权限层。权限判定由 security.permission_hook
    在 dispatch 路径上完成；这里只保证即使 hook 被绕过，工具也无法越界。

    越界时抛出 ValueError，由调用它的工具函数捕获并转成错误字符串。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """执行一条 shell 命令。"""
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def read_file(path: str) -> str:
    """读取一个文件。"""
    try:
        return safe_path(path).read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """把文件中的 old_text 替换为 new_text。"""
    try:
        file_path = safe_path(path)
        content = file_path.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: old_text not found in {path}"
        new_content = content.replace(old_text, new_text)
        file_path.write_text(new_content, encoding="utf-8")
        return (
            f"Edited {path}: replaced {len(old_text)} bytes "
            f"with {len(new_text)} bytes"
        )
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def write_file(path: str, content: str) -> str:
    """把 content 写入文件。"""
    try:
        file_path = safe_path(path)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def run_glob(pattern: str) -> str:
    """按 glob 模式列出工作目录中的文件。"""
    matches = sorted(set(glob.glob(pattern, root_dir=WORKDIR, recursive=True)))
    shown = matches[:200]
    if len(matches) > 200:
        shown.append("... (more matches omitted; narrow the pattern)")
    return "\n".join(shown) if shown else "(no matches)"


# ============================================================
# 定义（给模型看的说明书）
# ============================================================

TOOLS = [
    {
        "name": "bash",
        "description": "Execute a bash command in the working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "Read a file in the working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "edit_file",
        "description": "Edit a file in the working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path"},
                "old_text": {"type": "string", "description": "Text to replace"},
                "new_text": {"type": "string", "description": "New text"},
            },
            "required": ["path", "old_text", "new_text"],
        },
    },
    {
        "name": "write_file",
        "description": "Write to a file in the working directory.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "glob_files",
        "description": "List files in the working directory matching a pattern.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
            },
            "required": ["pattern"],
        },
    },
]

# 实现：工具名 -> 函数。必须与 TOOLS 一一对应。
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": read_file,
    "edit_file": edit_file,
    "write_file": write_file,
    "glob_files": run_glob,
}


# ============================================================
# 工具池组装（对应教程 s14 的 assemble_tool_pool）
# ============================================================

def assemble_tool_pool() -> tuple[list[dict], dict[str, Callable]]:
    """组装本轮要交给模型的工具池。

    返回 (tools, handlers)，两者来自**同一次快照** —— 这保证模型看到的
    工具一定都有对应实现。

    内置工具 + 技能工具（load_skill）。M4 接入 MCP 时也在这里合并，
    agent_loop 不需要任何改动。
    """
    tools = list(TOOLS) + skill_library.tool_defs()
    handlers = dict(TOOL_HANDLERS) | skill_library.handlers()

    names = [t["name"] for t in tools]
    if len(names) != len(set(names)) or set(names) != set(handlers):
        raise RuntimeError(f"工具池定义与实现不一致: tools={names}, handlers={sorted(handlers)}")
    return tools, handlers
