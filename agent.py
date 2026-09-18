"""最小可运行的 Agent —— myharness 的起点。

它只做三件事：
  1. 把用户输入发给模型
  2. 模型要求调用工具时，执行工具并把结果回填
  3. 模型不再调用工具时，这一轮结束

运行：
    python agent.py
"""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

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


def read_file(path: str) -> str:
    """读一个文件。出错就返回错误字符串，绝不抛异常。"""
    try:
        return (WORKDIR / path).read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


# 定义：模型靠这个知道"有哪些工具、参数长什么样"
TOOLS = [
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
]

# 实现：工具名 -> 函数
HANDLERS = {"read_file": read_file}

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
            print(f"  [tool] {block.name}({block.input})")
            handler = HANDLERS.get(block.name)
            try:
                output = (
                    str(handler(**block.input))
                    if handler
                    else f"Unknown tool: {block.name}"
                )
            except Exception as exc:
                # 工具报错只变成一条结果，绝不让程序崩掉
                output = f"Error: {type(exc).__name__}: {exc}"
            print(f"  [result] {output[:200]}")
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": output,
            })
        messages.append({"role": "user", "content": results})

    print("  [warn] 达到最大轮次，强制结束")


def main() -> None:
    print(f"myharness | model={MODEL}")
    print(f"workdir={WORKDIR}")
    print("输入问题，q 退出\n")

    history: list = []
    while True:
        try:
            query = input("you >> ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in {"q", "exit", ""}:
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)

        # 打印模型最后一段文字
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(f"\nbot >> {block.text}\n")


if __name__ == "__main__":
    main()
