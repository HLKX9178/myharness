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

from myharness.config import WORKDIR, memory_store, skill_library
from myharness.mcp.registry import get_registry
from myharness.mcp.mock_servers import MOCK_SERVERS


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


def connect_mcp(name: str) -> str:
    """连接 MCP server（模型可调用的工具）。"""
    if name not in MOCK_SERVERS:
        available = ", ".join(MOCK_SERVERS.keys())
        return f"Error: Unknown server '{name}'. Available: {available}"

    registry = get_registry()
    if name in registry.clients:
        return f"MCP server '{name}' already connected"

    try:
        # 创建 mock server
        factory = MOCK_SERVERS[name]
        client = factory()

        # 添加到注册表
        registry.add_client(client)

        tool_names = ", ".join(tool["name"] for tool in client.tools)
        return (
            f"Connected to MCP server '{name}'. "
            f"Discovered {len(client.tools)} tools: {tool_names}"
        )
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


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
    {
        "name": "connect_mcp",
        "description": "Connect to an MCP server to access its tools. Available: docs, deploy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Server name",
                    "enum": list(MOCK_SERVERS.keys()),
                },
            },
            "required": ["name"],
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
    "connect_mcp": connect_mcp,
}


# ============================================================
# 工具池组装（对应教程 s14 的 assemble_tool_pool）
# ============================================================

def assemble_tool_pool() -> tuple[list[dict], dict[str, Callable]]:
    """组装本轮要交给模型的工具池。

    返回 (tools, handlers)，两者来自**同一次快照** —— 这保证模型看到的
    工具一定都有对应实现。

    内置工具 + 技能工具（load_skill）+ 记忆工具（remember）+ MCP 工具。
    """
    # 1. 内置工具
    builtin_tools = list(TOOLS)
    builtin_handlers = dict(TOOL_HANDLERS)

    # 2. 技能工具
    builtin_tools.extend(skill_library.tool_defs())
    builtin_handlers.update(skill_library.handlers())

    # 3. 记忆工具
    builtin_tools.extend(memory_store.tool_defs())
    builtin_handlers.update(memory_store.handlers())

    # 4. MCP 工具（动态合并）
    registry = get_registry()
    try:
        tools, handlers, _policies = registry.assemble_tool_pool(
            builtin_tools, builtin_handlers
        )
    except Exception as exc:
        # 合并失败（命名冲突、schema 非法等）直接抛出，
        # 让调用方（agent_loop）能看到错误并中止本轮
        raise RuntimeError(f"Failed to assemble MCP tool pool: {exc}") from exc

    # 5. 一致性检查
    names = [t["name"] for t in tools]
    if len(names) != len(set(names)) or set(names) != set(handlers):
        raise RuntimeError(
            f"工具池定义与实现不一致: tools={names}, handlers={sorted(handlers)}"
        )

    return tools, handlers
