"""终端交互入口。"""

from myharness.config import WORKDIR, provider
from myharness.loop import agent_loop
from myharness.ui import Color


def main() -> None:
    import sys
    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}", flush=True)
    print(f"{Color.BOLD}myharness{Color.RESET} | model={provider.get_model_name()}", flush=True)
    print(f"workdir={WORKDIR}", flush=True)
    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}", flush=True)
    print("输入问题，q 退出\n", flush=True)

    history: list = []
    while True:
        try:
            sys.stdout.flush()
            query = input(f"\n{Color.CYAN}{Color.BOLD}YOU >>{Color.RESET} ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in {"q", "exit", ""}:
            break

        history.append({"role": "user", "content": query})
        print()  # 空行分隔
        agent_loop(history)

        # 打印模型最后一段文字
        print(f"\n{Color.GREEN}{Color.BOLD}BOT >>{Color.RESET} ", end="")
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(f"{Color.GREEN}{block.text}{Color.RESET}")
        print(f"{Color.GRAY}{'-' * 60}{Color.RESET}")
