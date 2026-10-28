"""终端交互入口。"""

import sys
import argparse
import os
import time
from pathlib import Path

# 修复 Windows 终端编码问题
def _force_utf8(stream) -> None:
    """尽力把标准流切成 UTF-8。

    不是所有「标准流」都是 TextIOWrapper：pytest 会把 stdin 换成
    DontReadFromInput，pythonw / 管道 / 重定向也可能是别的对象，它们没有
    reconfigure。早先无条件调用让 `import myharness.cli` 在测试里直接崩，
    而导入一个 CLI 模块不该依赖终端的类型。
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass


if sys.platform == "win32":
    # 设置输入输出编码为 UTF-8
    for _stream in (sys.stdout, sys.stderr, sys.stdin):
        _force_utf8(_stream)
    # 设置环境变量让子进程也使用 UTF-8
    os.environ['PYTHONIOENCODING'] = 'utf-8'

from myharness.config import SCHEDULE_DIR, SESSION_DIR, WORKDIR, provider
from myharness.hooks import Block, submit_user_prompt
from myharness.loop import agent_loop
from myharness.session import Session
from myharness.ui import Color, StreamPrinter


def _print_final_reply(history: list) -> None:
    """打印模型本轮的文本回复。

    不能假设 history[-1] 一定是 assistant 回复：本轮可能因为 provider 调用
    失败而提前结束（见 loop.agent_loop），此时最后一条消息是用户输入。
    对字符串 content 直接迭代会逐个字符打印，所以这里必须先判断角色。
    """
    last = history[-1] if history else {}
    content = last.get("content") if isinstance(last, dict) else None

    texts: list[str] = []
    if isinstance(last, dict) and last.get("role") == "assistant" and isinstance(content, list):
        for block in content:
            # 支持字典格式（序列化后）和对象格式（兼容）
            if isinstance(block, dict):
                if block.get("type") == "text":
                    texts.append(str(block.get("text", "")))
            elif getattr(block, "type", None) == "text":
                texts.append(str(block.text))

    answer = "\n".join(texts)
    print(f"\n{Color.GREEN}{Color.BOLD}BOT >>{Color.RESET} ", end="")
    if answer:
        print(f"{Color.GREEN}{answer}{Color.RESET}")
    else:
        print(f"{Color.GRAY}(本轮没有文本回复){Color.RESET}")
    print(f"{Color.GRAY}{'-' * 60}{Color.RESET}")


def _session_dirs() -> list[Path]:
    """所有可能存放会话的目录：交互会话 + 每个定时任务的留档。"""
    dirs = [SESSION_DIR]
    if SCHEDULE_DIR.exists():
        dirs.extend(sorted(path for path in SCHEDULE_DIR.iterdir() if path.is_dir()))
    return dirs


def _collect_sessions() -> list[dict]:
    """交互会话与定时任务会话合并，按更新时间倒序。"""
    entries: list[dict] = []
    for directory in _session_dirs():
        source = "" if directory == SESSION_DIR else directory.name
        for session in Session.list_sessions(directory):
            entries.append({**session, "source": source, "dir": directory})
    entries.sort(key=lambda entry: entry["updated_at"], reverse=True)
    return entries


def _format_time(stamp: str) -> str:
    """把 ISO 时间戳压成「2026-10-27 19:13」，列表里不需要微秒。"""
    return stamp[:16].replace("T", " ")


def _load_session(session_id: str) -> Session:
    """按会话 ID 加载；`last` 表示最近更新的一条。

    交互会话与定时任务的留档都能打开。恢复现场时记不住
    `session_20261027_171926` 这种 ID 很正常，所以留了 `last` 这个别名。
    """
    if session_id == "last":
        entries = _collect_sessions()
        if not entries:
            raise FileNotFoundError(session_id)
        # 跳过「打开过、没提问就退出」的空壳，否则刚启动又退出一次，
        # 用户再 --resume last 会得到一个空对话，看起来像坏了
        latest = next(
            (entry for entry in entries if entry["message_count"]),
            entries[0],
        )
        return Session.load(latest["dir"], latest["id"])

    for directory in _session_dirs():
        if (directory / f"{session_id}.json").exists():
            return Session.load(directory, session_id)
    raise FileNotFoundError(session_id)


def _print_sessions() -> None:
    """列出所有可恢复的会话（`--list-sessions`）。"""
    entries = _collect_sessions()
    # 空会话是「打开过、没提问就退出」留下的壳，恢复它没有意义，只会挤占列表
    visible = [entry for entry in entries if entry["message_count"]]
    if not visible:
        print("没有保存的会话")
        return

    print(f"{Color.BOLD}已保存的会话：{Color.RESET}")
    for index, session in enumerate(visible, 1):
        tag = f" [定时 {session['source']}]" if session["source"] else ""
        print(
            f"{index}. {session['id']}{tag}  "
            f"{_format_time(session['updated_at'])}  "
            f"{session['message_count']} 条"
        )
        if session["preview"]:
            print(f"   {Color.GRAY}{session['preview']}{Color.RESET}")

    hidden = len(entries) - len(visible)
    if hidden:
        print(f"{Color.GRAY}（另有 {hidden} 条空会话未列出）{Color.RESET}")
    print(
        f"\n{Color.GRAY}恢复：python -m myharness --resume <id>"
        f"（最近一条用 --resume last）{Color.RESET}"
    )


def _print_schedules() -> None:
    """打印已登记的定时任务（`--list-jobs`）。"""
    from myharness.runtime import scheduler as runtime_scheduler

    tasks = runtime_scheduler.get_scheduler().list_tasks()
    if not tasks:
        print("没有定时任务。让 agent 调用 schedule_task，或参考 README 的「定时任务」。")
        return

    print(f"{Color.BOLD}定时任务：{Color.RESET}")
    for task in tasks:
        state = "enabled" if task.enabled else "disabled"
        print(f"- {task.name} ({task.task_id}) [{state}] cron={task.cron_expr}")
        print(f"  next_run={task.next_run}")
        if task.last_status:
            print(f"  last={task.last_status} at {task.last_run}")
        if task.last_session:
            print(f"  session={task.last_session}  (python -m myharness --resume {task.last_session})")
        if task.last_error:
            print(f"  {Color.RED}error={task.last_error}{Color.RESET}")
        print(f"  prompt={task.prompt[:80]}")


def _run_scheduler_forever() -> None:
    """前台运行调度器（`--run-jobs`），Ctrl-C 结束。

    交互式会话里不启动调度线程：foreground 与 background 各跑一轮 agent 会
    互相污染 hook / guards 的进程级状态。调度只在**这个**进程里执行。
    """
    from myharness.runtime import scheduler as runtime_scheduler
    from myharness.runtime.background import get_manager

    scheduler = runtime_scheduler.get_scheduler()
    tasks = scheduler.list_tasks()
    if not tasks:
        print("没有定时任务，退出。先让 agent 调用 schedule_task 登记一条。")
        return

    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}")
    print(f"{Color.BOLD}调度器{Color.RESET} | {len(tasks)} 个任务 | state={scheduler.state_file}")
    for task in tasks:
        state = "enabled" if task.enabled else "disabled"
        print(f"  - {task.name} [{state}] cron={task.cron_expr} next={task.next_run}")
    print(f"{Color.GRAY}Ctrl-C 结束{Color.RESET}")
    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}")
    # 常驻进程的输出可能被重定向到文件：不 flush 就迟迟看不到启动信息
    sys.stdout.flush()

    scheduler.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("\n停止调度中……")
    finally:
        scheduler.stop()
        # 已经提交到线程池的任务等它跑完再退出（M5 的优雅关闭）
        get_manager().shutdown(wait=True)
    print(f"{Color.GRAY}已停止{Color.RESET}")


def main() -> None:
    # 解析命令行参数
    parser = argparse.ArgumentParser(description="myharness - Agent Harness")
    parser.add_argument(
        "--resume",
        type=str,
        help="恢复指定的会话 ID；用 last 表示最近一条有内容的会话",
    )
    parser.add_argument(
        "--list-sessions",
        action="store_true",
        help="列出所有可恢复的会话（含定时任务的留档，带首条消息预览）",
    )
    parser.add_argument(
        "--list-jobs",
        action="store_true",
        help="列出定时任务及其最近一次运行结果",
    )
    parser.add_argument(
        "--run-jobs",
        action="store_true",
        help="前台运行调度器，到点执行定时任务（Ctrl-C 结束）",
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="关闭流式输出，等整轮结束后一次打印（排查显示问题时用）",
    )
    parser.add_argument(
        "-w",
        "--workspace",
        type=str,
        default=None,
        help=(
            "工作目录，同时也是权限边界。可以是仓库之外的目录。"
            "默认取 WORKDIR 环境变量，未设置时用仓库根目录。"
        ),
    )
    args = parser.parse_args()

    # 列出会话
    if args.list_sessions:
        _print_sessions()
        return

    if args.list_jobs:
        _print_schedules()
        return

    if args.run_jobs:
        _run_scheduler_forever()
        return

    # --workspace：把整个 Agent 换到另一个目录去工作（权限边界随之移动）。
    # 非法值直接报错，不静默回退 —— 与 config._env_dir 同一约定：
    # 「我明明指定了却没生效」比直接报错难查得多。
    workspace: Path | None = None
    if args.workspace:
        candidate = Path(args.workspace).expanduser().resolve()
        if not candidate.is_dir():
            print(
                f"{Color.RED}错误：工作目录不存在或不是目录：{candidate}{Color.RESET}"
            )
            return
        workspace = candidate

    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}", flush=True)
    print(f"{Color.BOLD}myharness{Color.RESET} | model={provider.get_model_name()}", flush=True)
    print(f"workdir={workspace or WORKDIR}", flush=True)
    print(f"{Color.GRAY}{'=' * 60}{Color.RESET}", flush=True)

    # 创建或恢复会话
    if args.resume:
        try:
            session = _load_session(args.resume)
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

        # 流式展示：模型一开始吐字就打印，不必等整轮结束。
        # printer.started 是「这一轮到底有没有正文」的唯一判据 —— 模型可能整轮
        # 只调工具，那就交给 _print_final_reply 说明「本轮没有文本回复」。
        printer = StreamPrinter(enabled=not args.no_stream)
        # --no-stream 时干脆不传回调：provider 走非流式请求，不必先流再丢弃
        agent_loop(
            history,
            workspace=workspace,
            on_text=None if args.no_stream else printer,
        )

        # 保存会话
        session.save()

        if printer.started:
            print(f"{Color.GRAY}{'-' * 60}{Color.RESET}")
        else:
            # 没有流式正文（只调了工具，或 provider 在首片之前就失败了）
            _print_final_reply(history)

    # 退出时保存最终状态。空会话不落盘 —— 打开就退出不该在 sessions/ 里
    # 留一个空壳（历史上 39 个存档里有 11 个是这么来的，列表还得单独过滤）。
    if session.messages:
        session.save()
        print(f"\n{Color.GRAY}会话已保存: {session.session_id}{Color.RESET}")
    else:
        print(f"\n{Color.GRAY}未产生对话，未创建会话{Color.RESET}")

