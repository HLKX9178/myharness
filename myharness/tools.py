"""工具集。

每个工具由两部分组成，**必须成对出现**，少一个都不行（见 AGENTS.md 契约 3）：

  1. 实现 —— 真正执行的函数
  2. 定义 —— 放进 TOOLS，给模型看的 JSON Schema

契约（见 AGENTS.md 契约 1）：所有工具函数只返回字符串，
出错返回 "Error: ..."，绝不抛异常。
"""

import glob
import subprocess

from myharness.config import WORKDIR


# ============================================================
# 实现
# ============================================================

def safe_path(p: str):
    """把相对路径 p 转成绝对路径，并确保它落在工作目录内。

    这是内部辅助函数，不是工具。越界时抛出 ValueError，由调用它的
    工具函数捕获并转成错误字符串。
    """
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str) -> str:
    """执行一条 shell 命令。"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=WORKDIR,
            capture_output=True,
            text=True,
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
        return (WORKDIR / path).read_text(encoding="utf-8")
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
    return "\n".join(shown)


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
