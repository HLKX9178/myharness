"""最小可运行的 Agent —— myharness 的起点。

它只做三件事：
  1. 把用户输入发给模型
  2. 模型要求调用工具时，执行工具并把结果回填
  3. 模型不再调用工具时，这一轮结束

运行：
    python agent.py
"""

import os
import subprocess
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# ---- 终端颜色 ----
class Color:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"      # 用户输入
    GREEN = "\033[32m"     # 模型输出
    YELLOW = "\033[33m"    # 工具调用
    RED = "\033[31m"       # 错误
    GRAY = "\033[90m"      # 分隔线

# ---- 配置 ----
load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# 这个 Agent 的工作目录。
# 目前指向仓库根目录，方便它读自己的 README。
# 后续会改成独立的 workspace/，避免 Agent 改到自己的源码。
WORKDIR = Path(__file__).resolve().parent
MODEL = os.environ["MODEL_ID"]
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."

# ============================================================
# 工具 = 定义（给模型看的说明书） + 实现（真正执行的代码）
# 这两个必须成对出现，少一个都不行。
# ============================================================
def safe_path(p:str) -> Path:
    """把相对路径 p 转成绝对路径，并确保它在工作目录里。"""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def run_bash(command: str) -> str:
    """执行一个 bash 命令。出错就返回错误字符串，绝不抛异常"""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    """危险指令拒绝执行"""
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, errors="replace",
                           timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"

def read_file(path: str) -> str:
    """读一个文件。出错就返回错误字符串，绝不抛异常。"""
    try:
        return (WORKDIR / path).read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"
    
def edit_file(path: str, old_text: str, new_text: str) -> str:
    """编辑一个文件。出错就返回错误字符串，不抛异常"""
    try:
        file_path = safe_path(path)
        content = file_path.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: old_text not found in {path}"
        new_content = content.replace(old_text, new_text)
        file_path.write_text(new_content, encoding="utf-8")
        return f"Edited {path}: replaced {len(old_text)} bytes with {len(new_text)} bytes"
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"
     
def write_file(path: str, content: str) -> str:
    """写一个文件。出错就返回错误字符串，不抛异常"""
    try:
        file_path = safe_path(path)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"

def run_glob(pattern):
    import glob as g
    matches = sorted(set(g.glob(
        pattern, root_dir=WORKDIR, recursive=True)))
    shown = matches[:200]
    if len(matches) > 200:
        shown.append("... (more matches omitted; narrow the pattern)")
    return "\n".join(shown)

# 定义：模型靠这个知道"有哪些工具、参数长什么样"
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
    }
]

# 实现：工具名 -> 函数
TOOL_HANDLERS = {
    "bash": run_bash,
    "read_file": read_file,
    "edit_file": edit_file,
    "write_file": write_file,
    "glob_files": run_glob,
}

MAX_TURNS = 20  # 防止模型无限循环，这是最重要的一行保险


def agent_loop(messages: list) -> None:
    """反复「问模型 -> 执行工具 -> 回填结果」，直到模型不再用工具。"""
    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL,
            system=SYSTEM,
            messages=messages,
            tools=TOOLS,
            max_tokens=2000,
        )
        messages.append({"role": "assistant", "content": response.content})

        tool_calls = [b for b in response.content if b.type == "tool_use"]
        if not tool_calls:
            return  # 模型不再调用工具 -> 这一轮结束

        results = []
        for block in tool_calls:
            print(f"{Color.YELLOW}  [tool] {block.name}({block.input}){Color.RESET}")
            handler = TOOL_HANDLERS.get(block.name)
            try:
                output = (
                    str(handler(**block.input))
                    if handler
                    else f"Unknown tool: {block.name}"
                )
            except Exception as exc:
                # 工具报错只变成一条结果，绝不让程序崩掉
                output = f"Error: {type(exc).__name__}: {exc}"
            print(f"{Color.GRAY}  [result] {output[:200]}{Color.RESET}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})

    print(f"{Color.RED}  [warn] 达到最大轮次，强制结束{Color.RESET}")


def main() -> None:
    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}")
    print(f"{Color.BOLD}myharness{Color.RESET} | model={MODEL}")
    print(f"workdir={WORKDIR}")
    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}")
    print("输入问题，q 退出\n")

    history: list = []
    while True:
        try:
            query = input(f"\n{Color.CYAN}{Color.BOLD}🧑 YOU >>{Color.RESET} ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in {"q", "exit", ""}:
            break

        history.append({"role": "user", "content": query})
        print()  # 空行分隔
        agent_loop(history)

        # 打印模型最后一段文字
        print(f"\n{Color.GREEN}{Color.BOLD}🤖 BOT >>{Color.RESET} ", end="")
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(f"{Color.GREEN}{block.text}{Color.RESET}")
        print(f"{Color.GRAY}{'-' * 60}{Color.RESET}")


if __name__ == "__main__":
    main()
