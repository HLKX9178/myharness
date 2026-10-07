"""终端交互入口。"""

import sys
import argparse

from myharness.config import WORKDIR, SESSION_DIR, provider
from myharness.hooks import Block, submit_user_prompt
from myharness.loop import agent_loop
from myharness.session import Session
from myharness.ui import Color


def main() -> None:
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="myharness - Agent Harness")
    parser.add_argument(
        "--resume",
        type=str,
        help="恢复指定的会话 ID（例如：session_20260927_171926）",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="列出所有已保存的会话",
    )
    args = parser.parse_args()

    # 列出会话
    if args.list_sessions:
        sessions = Session.list_sessions(SESSION_DIR)
        if not sessions:
            print("没有保存的会话")
            return

        print(f"{Color.BOLD}已保存的会话：{Color.RESET}")
        for i, sess in enumerate(sessions, 1):
            print(f"{i}. {sess['id']} (更新于 {sess['updated_at']})")
        return

    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}", flush=True)
    print(f"{Color.BOLD}myharness{Color.RESET} | model={provider.get_model_name()}", flush=True)
    print(f"workdir={WORKDIR}", flush=True)
    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}", flush=True)

    # 创建或恢复会话
    if args.resume:
        try:
            session = Session.load(SESSION_DIR, args.resume)
            print(f"session_id={session.session_id} (恢复)", flush=True)
            print(f"已加载 {len(session.messages)} 条消息", flush=True)
        except FileNotFoundError:
            print(f"{Color.RED}错误：会话 {args.resume} 不存在{Color.RESET}")
            return
    else:
        session = Session(SESSION_DIR)
        print(f"session_id={session.session_id} (新建)", flush=True)

    print("输入问题，q 退出\n", flush=True)

    history: list = session.messages
    while True:
        try:
            sys.stdout.flush()
            query = input(f"\n{Color.CYAN}{Color.BOLD}YOU >>{Color.RESET} ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in {"q", "exit", ""}:
            break

        # UserPromptSubmit：hook 可注入上下文，或拒绝这次输入
        submitted = submit_user_prompt(query)
        if isinstance(submitted, Block):
            print(f"{Color.RED}  [blocked] {submitted.reason}{Color.RESET}")
            continue

        session.add_message("user", submitted)
        print()  # 空行分隔
        agent_loop(history)

        # 保存会话
        session.save()

        # 打印模型最后一段文字
        print(f"\n{Color.GREEN}{Color.BOLD}BOT >>{Color.RESET} ", end="")
        for block in history[-1]["content"]:
            if getattr(block, "type", None) == "text":
                print(f"{Color.GREEN}{block.text}{Color.RESET}")
        print(f"{Color.GRAY}{'-' * 60}{Color.RESET}")

    # 退出时保存最终状态
    session.save()
    print(f"\n{Color.GRAY}会话已保存: {session.session_id}{Color.RESET}")

