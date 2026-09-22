"""终端交互入口。"""

from myharness.config import MODEL, WORKDIR
from myharness.loop import agent_loop
from myharness.ui import Color


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
