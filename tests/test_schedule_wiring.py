"""runtime 的三条接入路径：便捷函数、模型工具、命令行入口。

这个模块此前是「只有库、没有入口」：`schedule_task()` 调 `get_scheduler()`
不传 state_file，单例为空时直接 RuntimeError；即便手动建好调度器，重启后
`load_state()` 也恢复不了 handler，只会打印 "Handler not found"。

这里的用例逐条钉住接入点，任何一条断开都会红。
"""

import sys
import threading
import time

import pytest

from myharness import cli, tools
from myharness.runtime import runner
from myharness.runtime import scheduler as runtime_scheduler
from myharness.runtime.scheduler import STATUS_OK, ScheduledTask, Scheduler


@pytest.fixture
def isolated_scheduler(tmp_path, monkeypatch):
    """把全局调度器的状态文件指到 tmp_path，避免污染真实 .myharness/。"""
    state_file = tmp_path / "schedules.json"
    monkeypatch.setattr(runtime_scheduler, "default_state_file", lambda: state_file)
    runtime_scheduler.reset_scheduler()
    yield state_file
    runtime_scheduler.reset_scheduler()


# ============================================================
# 便捷函数：不该要求调用方先准备好 state_file
# ============================================================

def test_schedule_task_works_without_state_file(isolated_scheduler):
    """`schedule_task()` 首次调用必须自己找到默认状态文件（旧版必崩）。"""
    task_id = runtime_scheduler.schedule_task("boot", "0 9 * * *", "check ci")

    assert task_id
    assert isolated_scheduler.exists(), "默认路径没有被用上"
    assert runtime_scheduler.get_scheduler().get(task_id).prompt == "check ci"


def test_get_scheduler_returns_same_instance(isolated_scheduler):
    """单例复用：两处登记落在同一个状态文件里。"""
    runtime_scheduler.schedule_task("a", "0 9 * * *", "first")
    runtime_scheduler.schedule_task("b", "0 10 * * *", "second")

    assert len(runtime_scheduler.get_scheduler().list_tasks()) == 2


# ============================================================
# 模型工具
# ============================================================

def test_tool_schedule_list_cancel(isolated_scheduler):
    """模型可以登记、查看、取消定时任务。"""
    created = tools.schedule_task("tool_job", "0 9 * * *", "summarize new commits")

    assert "Scheduled" in created
    assert "job_" in created
    assert "--run-jobs" in created, "必须告诉调用方谁来执行"

    listed = tools.list_schedules()
    assert "tool_job" in listed
    assert "enabled" in listed

    assert "Cancelled" in tools.cancel_schedule("tool_job")
    assert tools.list_schedules() == "No scheduled tasks"


def test_tool_schedule_task_returns_error_string_on_bad_cron(isolated_scheduler):
    """契约 1/4：工具出错返回字符串，不抛异常。"""
    result = tools.schedule_task("bad", "not a cron", "x")

    assert result.startswith("Error:")
    assert "Invalid cron" in result


def test_tool_pool_contains_schedule_tools():
    """工具定义与实现成对出现（契约 3），且真的进了工具池。"""
    tool_defs, handlers = tools.assemble_tool_pool()
    names = {definition["name"] for definition in tool_defs}

    expected = {"schedule_task", "list_schedules", "cancel_schedule"}
    assert expected <= names
    assert expected <= set(handlers)


# ============================================================
# 命令行入口
# ============================================================

def test_cli_list_jobs(isolated_scheduler, monkeypatch, capsys):
    """`--list-jobs` 能列出任务。"""
    runtime_scheduler.schedule_task("cli_job", "0 9 * * *", "daily report")

    monkeypatch.setattr(sys, "argv", ["myharness", "--list-jobs"])
    cli.main()

    out = capsys.readouterr().out
    assert "cli_job" in out
    assert "next_run" in out


def test_cli_run_jobs_exits_when_nothing_scheduled(isolated_scheduler, monkeypatch, capsys):
    """`--run-jobs` 没有任务时立即退出，不会空转挂住终端。"""
    monkeypatch.setattr(sys, "argv", ["myharness", "--run-jobs"])
    cli.main()

    assert "没有定时任务" in capsys.readouterr().out


def test_cli_can_resume_a_scheduled_session(tmp_path, monkeypatch):
    """定时任务留下的会话能被 `--resume` 打开，不是只能去翻 JSON。"""
    from myharness.session import Session

    scheduled = tmp_path / "scheduled"
    session_dir = scheduled / "job_x"
    session_dir.mkdir(parents=True)

    original = Session(session_dir)
    original.add_message("user", "定时任务里的对话")
    original.save()

    monkeypatch.setattr(cli, "SCHEDULE_DIR", scheduled)

    loaded = cli._load_session(original.session_id)
    assert loaded.messages[0]["content"] == "定时任务里的对话"

    with pytest.raises(FileNotFoundError):
        cli._load_session("session_does_not_exist")


# ============================================================
# 执行器：一轮完整的 agent 会话
# ============================================================

def test_runner_runs_prompt_in_a_fresh_session(tmp_path, monkeypatch):
    """到期任务在新会话里跑一轮，结束后留档可查。"""
    import myharness.config as config_module
    import myharness.loop as loop_module

    monkeypatch.setattr(config_module, "SCHEDULE_DIR", tmp_path / "scheduled")

    seen: list[tuple[list, object]] = []

    def fake_agent_loop(messages, workspace=None):
        from myharness.loop import LoopOutcome

        seen.append((list(messages), workspace))
        messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": "all done"}]}
        )
        return LoopOutcome("completed", 1, 100)

    monkeypatch.setattr(loop_module, "agent_loop", fake_agent_loop)

    task = ScheduledTask(
        task_id="job_x",
        name="x",
        cron_expr="0 9 * * *",
        prompt="check the build",
    )
    result = runner.execute_task(task)

    assert seen[0][0][0] == {"role": "user", "content": "check the build"}
    assert result.output == "all done"
    assert (tmp_path / "scheduled" / "job_x" / f"{result.session_id}.json").exists()


def test_runner_reports_failure_when_model_call_fails(tmp_path, monkeypatch):
    """provider 失败时 agent_loop 只是优雅返回，执行器必须把它判成失败。

    否则任务被记成 ok，定时任务静默停摆而 list_schedules 显示一切正常。
    """
    import myharness.config as config_module
    import myharness.loop as loop_module
    from myharness.loop import LoopOutcome

    monkeypatch.setattr(config_module, "SCHEDULE_DIR", tmp_path / "scheduled")

    def failed_loop(messages, workspace=None):
        # 模拟 provider 报错后 agent_loop 提前收场：一条 assistant 都没有，
        # 但**明确说了**自己是失败退出
        return LoopOutcome("provider_error", 1, 0, "boom")

    monkeypatch.setattr(loop_module, "agent_loop", failed_loop)

    task = ScheduledTask(task_id="job_f", name="f", cron_expr="0 9 * * *", prompt="p")
    with pytest.raises(RuntimeError, match="did not complete \\(provider_error\\)"):
        runner.execute_task(task)


def test_runner_reports_failure_when_loop_hits_max_turns(tmp_path, monkeypatch):
    """跑满了轮次也算没干完 —— 哪怕过程中说过几句话。

    旧实现按「有没有产生 assistant 消息」判断，这种任务会被记成 ok：
    定时任务每天空转一圈、状态里全是绿的，比直接报错更难发现。
    """
    import myharness.config as config_module
    import myharness.loop as loop_module
    from myharness.loop import LoopOutcome

    monkeypatch.setattr(config_module, "SCHEDULE_DIR", tmp_path / "scheduled")

    def stuck_loop(messages, workspace=None):
        messages.append(
            {"role": "assistant", "content": [{"type": "text", "text": "我还在看"}]}
        )
        return LoopOutcome("no_progress", 9, 1234, "`read_file` 连续 5 次返回相同内容")

    monkeypatch.setattr(loop_module, "agent_loop", stuck_loop)

    task = ScheduledTask(task_id="job_s", name="s", cron_expr="0 9 * * *", prompt="p")
    with pytest.raises(RuntimeError, match="no_progress"):
        runner.execute_task(task)


def test_runner_serializes_jobs(tmp_path, monkeypatch):
    """同一进程内不并发跑两轮 agent（guards/hooks 是进程级状态）。"""
    import myharness.config as config_module
    import myharness.loop as loop_module

    monkeypatch.setattr(config_module, "SCHEDULE_DIR", tmp_path / "scheduled")

    active = 0
    peak = 0
    guard = threading.Lock()

    def fake_agent_loop(messages, workspace=None):
        from myharness.loop import LoopOutcome

        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        # 必须报告 completed，否则执行器会（正确地）判为失败
        messages.append({"role": "assistant", "content": [{"type": "text", "text": "ok"}]})
        return LoopOutcome("completed", 1, 100)

    monkeypatch.setattr(loop_module, "agent_loop", fake_agent_loop)

    task = ScheduledTask(task_id="job_s", name="s", cron_expr="0 9 * * *", prompt="p")
    threads = [
        threading.Thread(target=runner.execute_task, args=(task,)) for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert peak == 1


# ============================================================
# 端到端：登记 → 到点 → 执行 → 状态可读
# ============================================================

def test_scheduled_job_runs_end_to_end(isolated_scheduler, tmp_path, monkeypatch):
    """用假执行器跑通全链路：登记、到期、执行、状态写回、重新加载。"""
    import json

    executed: list[str] = []

    def executor(task):
        executed.append(task.prompt)

    scheduler = Scheduler(isolated_scheduler, executor=executor)
    task_id = scheduler.schedule("e2e", "* * * * *", "hello")
    with scheduler._lock:
        scheduler.tasks[task_id].next_run = "2000-01-01T00:00:00"

    scheduler.run_pending()

    task = scheduler.get(task_id)
    deadline = time.monotonic() + 5
    while task.last_status != STATUS_OK and time.monotonic() < deadline:
        time.sleep(0.02)

    assert executed == ["hello"]
    assert task.last_status == STATUS_OK

    saved = json.loads(isolated_scheduler.read_text(encoding="utf-8"))
    assert saved[task_id]["last_status"] == STATUS_OK
    assert saved[task_id]["prompt"] == "hello"
