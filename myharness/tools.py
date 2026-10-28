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
import time
from pathlib import Path
from typing import Any, Callable

from myharness import config
from myharness.config import memory_store, skill_library
from myharness.mcp.registry import get_registry
from myharness.mcp.mock_servers import MOCK_SERVERS


# ============================================================
# 实现
# ============================================================

def _get_workdir(workspace: Path | None = None) -> Path:
    """获取工作目录。

    Args:
        workspace: 显式传入的工作目录；None 时回退到全局 config.WORKDIR

    优先使用显式传入的 workspace（多 Agent 隔离场景），
    未传入时回退到 config.get_workdir()（向后兼容）。
    """
    return workspace if workspace is not None else config.get_workdir()


def safe_path(p: str, workspace: Path | None = None) -> Path:
    """把相对路径 p 转成绝对路径，并确保它落在工作目录内。

    这是**兜底防线**，不是权限层。权限判定由 security.permission_hook
    在 dispatch 路径上完成；这里只保证即使 hook 被绕过，工具也无法越界。

    越界时抛出 ValueError，由调用它的工具函数捕获并转成错误字符串。

    Args:
        p: 相对路径
        workspace: 工作目录；None 时使用全局 WORKDIR
    """
    workdir = _get_workdir(workspace)
    path = (workdir / p).resolve()
    if not path.is_relative_to(workdir):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def run_bash(command: str, workspace: Path | None = None) -> str:
    """
    执行一条 shell 命令（支持流式输出）。

    对于长时运行的命令，会实时显示输出进度。
    """
    try:
        workdir = _get_workdir(workspace)

        # 使用 Popen 进行流式输出
        process = subprocess.Popen(
            command,
            shell=True,
            cwd=workdir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,  # 行缓冲
        )

        output_lines = []
        max_lines = 2000  # 最多保留 2000 行输出

        # 实时读取输出（带超时保护）
        import threading
        import queue

        output_queue = queue.Queue()

        def read_output():
            """在单独线程中读取输出，避免阻塞"""
            try:
                for line in process.stdout:
                    output_queue.put(("line", line.rstrip()))
            except Exception as e:
                output_queue.put(("error", str(e)))
            finally:
                output_queue.put(("done", None))

        # 启动读取线程
        reader_thread = threading.Thread(target=read_output, daemon=True)
        reader_thread.start()

        # 从队列读取输出，带超时
        timeout = 120  # 总超时 120 秒
        start_time = time.time()

        while True:
            remaining_time = timeout - (time.time() - start_time)
            if remaining_time <= 0:
                # 超时，强制终止进程
                process.kill()
                output_lines.append("(command timed out after 120s)")
                break

            try:
                msg_type, content = output_queue.get(timeout=min(1, remaining_time))

                if msg_type == "line":
                    # 实时打印到终端
                    print(f"  {content}")
                    output_lines.append(content)

                    # 限制内存占用
                    if len(output_lines) > max_lines:
                        output_lines = output_lines[-max_lines:]

                elif msg_type == "error":
                    output_lines.append(f"(read error: {content})")
                    break

                elif msg_type == "done":
                    break

            except queue.Empty:
                # 检查进程是否还在运行
                if process.poll() is not None:
                    # 进程已结束，再等一下让线程读完剩余输出
                    reader_thread.join(timeout=1)
                    break

        # 等待进程结束
        try:
            return_code = process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = -1

        # 组装结果
        result = "\n".join(output_lines)

        if return_code != 0:
            result += f"\n(exit code: {return_code})"

        return result[:50000] if result else "(no output)"

    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


def read_file(path: str, workspace: Path | None = None) -> str:
    """读取一个文件。"""
    try:
        return safe_path(path, workspace).read_text(encoding="utf-8")
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def edit_file(path: str, old_text: str, new_text: str, workspace: Path | None = None) -> str:
    """把文件中的 old_text 替换为 new_text。"""
    try:
        file_path = safe_path(path, workspace)
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


def write_file(path: str, content: str, workspace: Path | None = None) -> str:
    """把 content 写入文件。"""
    try:
        file_path = safe_path(path, workspace)
        file_path.write_text(content, encoding="utf-8")
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def run_glob(pattern: str, workspace: Path | None = None) -> str:
    """按 glob 模式列出工作目录中的文件。"""
    matches = sorted(set(glob.glob(pattern, root_dir=_get_workdir(workspace), recursive=True)))
    shown = matches[:200]
    if len(matches) > 200:
        shown.append("... (more matches omitted; narrow the pattern)")
    return "\n".join(shown) if shown else "(no matches)"


def connect_mcp(name: str, workspace: Path | None = None) -> str:
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
# Teams 工具集（多 Agent 协作）
# ============================================================

# 全局 Team Manager 实例
_active_team = None


def _preview(value: Any, limit: int = 100) -> str:
    """把任务结果 / 错误信息截成一行预览（Task.result 是 Any，不一定是 str）。"""
    text = str(value)
    return text[:limit] + "..." if len(text) > limit else text


def spawn_team(num_agents: int, workspace: Path | None = None) -> str:
    """
    启动多个 AI Agent 组成团队并行工作。

    Args:
        num_agents: Agent 数量（1-10）
        workspace: 工作区路径

    Returns:
        状态信息字符串
    """
    global _active_team

    try:
        # 检查是否已有活跃团队
        if _active_team is not None:
            return "Error: Team already active. Use stop_team() first."

        # 验证参数
        if not isinstance(num_agents, int) or num_agents < 1 or num_agents > 10:
            return "Error: num_agents must be between 1 and 10"

        from myharness.teams import AgentManager

        workdir = _get_workdir(workspace)
        team_dir = workdir / ".team"

        # 创建并启动团队
        _active_team = AgentManager(team_dir=team_dir, num_agents=num_agents)
        _active_team.start_team()

        return f"Team started: {num_agents} agents ready. Use create_task() to assign work."

    except Exception as exc:
        _active_team = None
        return f"Error: Failed to start team: {type(exc).__name__}: {exc}"


def stop_team() -> str:
    """
    停止当前活跃的 Agent 团队。

    Returns:
        状态信息字符串
    """
    global _active_team

    try:
        if _active_team is None:
            return "Error: No active team to stop"

        _active_team.stop_team()
        _active_team = None

        return "Team stopped successfully"

    except Exception as exc:
        return f"Error: Failed to stop team: {type(exc).__name__}: {exc}"


def create_task(title: str, description: str) -> str:
    """
    创建任务并分配给团队中的某个 Agent。

    Args:
        title: 任务标题
        description: 任务详细描述

    Returns:
        任务 ID 或错误信息
    """
    try:
        if _active_team is None:
            return "Error: No active team. Use spawn_team() first."

        task_id = _active_team.taskboard.create_task(title=title, description=description)

        return f"Task created: {task_id}. An agent will claim it soon."

    except Exception as exc:
        return f"Error: Failed to create task: {type(exc).__name__}: {exc}"


def list_tasks() -> str:
    """
    列出团队中的所有任务及其状态。

    Returns:
        任务列表（文本格式）
    """
    try:
        if _active_team is None:
            return "Error: No active team"

        tasks = _active_team.taskboard.list_tasks()

        if not tasks:
            return "No tasks in queue"

        # 用枚举做键，而不是字符串字面量：TaskStatus 的成员名与取值
        # 曾经对不上（"in_progress" vs "claimed"），字面量映射会静默退化成
        # [UNKNOWN]，用枚举后这类错位在类型层面就不可能出现。
        from myharness.teams.taskboard import TaskStatus

        status_icon = {
            TaskStatus.PENDING: "[PENDING]",
            TaskStatus.CLAIMED: "[IN_PROGRESS]",
            TaskStatus.COMPLETED: "[COMPLETED]",
            TaskStatus.FAILED: "[FAILED]",
        }

        lines = []
        for task in tasks:
            lines.append(f"{status_icon.get(task.status, '[UNKNOWN]')} {task.title}")

            if task.claimed_by:
                lines.append(f"   Agent: {task.claimed_by}")

            # 失败原因记在 error 字段，成功结果记在 result 字段
            # （见 TaskBoard.fail_task / complete_task），两者不能混用。
            if task.status is TaskStatus.FAILED and task.error:
                lines.append(f"   Error: {_preview(task.error)}")
            elif task.result:
                lines.append(f"   Result: {_preview(task.result)}")

        return "\n".join(lines)

    except Exception as exc:
        return f"Error: Failed to list tasks: {type(exc).__name__}: {exc}"


def get_task_result(task_id: str) -> str:
    """
    获取指定任务的结果。

    Args:
        task_id: 任务 ID

    Returns:
        任务结果或错误信息
    """
    try:
        if _active_team is None:
            return "Error: No active team"

        task = _active_team.taskboard.get_task(task_id)

        if task is None:
            return f"Error: Task {task_id} not found"

        from myharness.teams.taskboard import TaskStatus

        if task.status is TaskStatus.COMPLETED:
            return f"Task completed:\n{task.result or '(no result)'}"
        elif task.status is TaskStatus.FAILED:
            # fail_task 把原因写进 error 字段（result 保持 None），
            # 只读 result 会让失败原因永远显示成 "(no error details)"。
            return f"Task failed:\n{task.error or task.result or '(no error details)'}"
        elif task.status is TaskStatus.CLAIMED:
            return f"Task still in progress (claimed by {task.claimed_by})"
        else:
            return f"Task pending (status: {task.status.value})"

    except Exception as exc:
        return f"Error: Failed to get task result: {type(exc).__name__}: {exc}"


def send_team_message(to_agent: str, content: str) -> str:
    """
    向团队中的某个 Agent 发送消息。

    Args:
        to_agent: 目标 Agent ID
        content: 消息内容

    Returns:
        状态信息
    """
    try:
        if _active_team is None:
            return "Error: No active team"

        from_agent = "coordinator"  # 协调者（主 Agent）
        message_id = _active_team.message_queue.send(
            from_agent=from_agent,
            to_agent=to_agent,
            content=content
        )

        return f"Message sent to {to_agent} (ID: {message_id})"

    except Exception as exc:
        return f"Error: Failed to send message: {type(exc).__name__}: {exc}"


# ============================================================
# 定时任务
# ============================================================
#
# 调度任务的内容是「提示词」而不是函数指针：到期时在独立进程里跑一轮完整的
# agent。任务只在 `python -m myharness --run-jobs` 常驻的进程里执行 —— 交互式
# 会话不启动调度线程，因为 agent_loop 依赖若干进程级状态，前台跑一轮的同时后台
# 再跑一轮会互相污染（见 runtime/runner.py）。

def schedule_task(
    name: str,
    cron_expr: str,
    prompt: str,
    workspace: Path | None = None,
) -> str:
    """
    登记一条定时任务。

    Args:
        name: 任务名称（同名视为更新，不会重复登记）
        cron_expr: Cron 表达式（标准 5 字段）
        prompt: 到期时交给 agent 的提示词
        workspace: 工作目录，默认当前会话的工作目录

    Returns:
        登记结果（含 task_id 与下次触发时间）
    """
    try:
        from myharness.runtime import scheduler as runtime_scheduler

        task_id = runtime_scheduler.schedule_task(
            name=name,
            cron_expr=cron_expr,
            prompt=prompt,
            workspace=_get_workdir(workspace),
        )
        task = runtime_scheduler.get_scheduler().get(task_id)

        return (
            f"Scheduled: {task_id} name={name} cron={cron_expr} "
            f"next_run={task.next_run}\n"
            f"Runs only while the runner is alive: python -m myharness --run-jobs"
        )

    except Exception as exc:
        return f"Error: Failed to schedule task: {type(exc).__name__}: {exc}"


def list_schedules() -> str:
    """
    列出已登记的定时任务及其最近一次运行结果。

    Returns:
        任务列表（文本格式）
    """
    try:
        from myharness.runtime import scheduler as runtime_scheduler

        tasks = runtime_scheduler.get_scheduler().list_tasks()
        if not tasks:
            return "No scheduled tasks"

        lines = []
        for task in tasks:
            state = "enabled" if task.enabled else "disabled"
            lines.append(
                f"{task.task_id} [{state}] {task.name} cron={task.cron_expr} "
                f"next_run={task.next_run}"
            )
            if task.last_status:
                lines.append(f"   last: {task.last_status} at {task.last_run}")
            if task.last_error:
                lines.append(f"   error: {_preview(task.last_error)}")
            elif task.last_result:
                lines.append(f"   result: {_preview(task.last_result)}")
            lines.append(f"   prompt: {_preview(task.prompt)}")

        return "\n".join(lines)

    except Exception as exc:
        return f"Error: Failed to list schedules: {type(exc).__name__}: {exc}"


def cancel_schedule(ref: str) -> str:
    """
    取消一条定时任务。

    Args:
        ref: 任务 ID 或任务名称

    Returns:
        状态信息
    """
    try:
        from myharness.runtime import scheduler as runtime_scheduler

        if not runtime_scheduler.get_scheduler().cancel(ref):
            return f"Error: No such scheduled task: {ref}"
        return f"Cancelled scheduled task: {ref}"

    except Exception as exc:
        return f"Error: Failed to cancel schedule: {type(exc).__name__}: {exc}"


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
    {
        "name": "spawn_team",
        "description": "Start multiple AI agents to work in parallel on different tasks. Use this when you need to scale work across multiple independent tasks.",
        "input_schema": {
            "type": "object",
            "properties": {
                "num_agents": {
                    "type": "integer",
                    "description": "Number of agents to spawn (1-10)",
                    "minimum": 1,
                    "maximum": 10,
                },
            },
            "required": ["num_agents"],
        },
    },
    {
        "name": "stop_team",
        "description": "Stop the currently active agent team and clean up resources.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "create_task",
        "description": "Create a task for the agent team. An available agent will claim and execute it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Short task title",
                },
                "description": {
                    "type": "string",
                    "description": "Detailed task description with requirements and context",
                },
            },
            "required": ["title", "description"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List all tasks in the team queue with their status (pending/claimed/completed/failed).",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_task_result",
        "description": "Get the result of a completed task by its ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "string",
                    "description": "Task ID returned when the task was created",
                },
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "send_team_message",
        "description": "Send a message to a specific agent in the team.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to_agent": {
                    "type": "string",
                    "description": "Target agent ID",
                },
                "content": {
                    "type": "string",
                    "description": "Message content",
                },
            },
            "required": ["to_agent", "content"],
        },
    },
    {
        "name": "schedule_task",
        "description": (
            "Schedule a prompt to be run by this agent on a cron schedule. "
            "The prompt runs in a fresh session by `python -m myharness --run-jobs`. "
            "Scheduling the same name again updates the existing entry."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Unique job name (reuse a name to update it)",
                },
                "cron_expr": {
                    "type": "string",
                    "description": "Standard 5-field cron expression, e.g. '0 9 * * 1-5'",
                },
                "prompt": {
                    "type": "string",
                    "description": "Self-contained prompt to execute when the job fires",
                },
            },
            "required": ["name", "cron_expr", "prompt"],
        },
    },
    {
        "name": "list_schedules",
        "description": "List scheduled jobs with their next run time and last result.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "cancel_schedule",
        "description": "Cancel a scheduled job by its name or id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": "Job name or task id",
                },
            },
            "required": ["ref"],
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
    "spawn_team": spawn_team,
    "stop_team": stop_team,
    "create_task": create_task,
    "list_tasks": list_tasks,
    "get_task_result": get_task_result,
    "send_team_message": send_team_message,
    "schedule_task": schedule_task,
    "list_schedules": list_schedules,
    "cancel_schedule": cancel_schedule,
}


# ============================================================
# 工具池组装（对应教程 s14 的 assemble_tool_pool）
# ============================================================

def assemble_tool_pool(workspace: Path | None = None) -> tuple[list[dict], dict[str, Callable]]:
    """组装本轮要交给模型的工具池。

    返回 (tools, handlers)，两者来自**同一次快照** —— 这保证模型看到的
    工具一定都有对应实现。

    内置工具 + 技能工具（load_skill）+ 记忆工具（remember）+ MCP 工具。

    Args:
        workspace: 工作目录（传递给工具函数）；None 时使用全局 WORKDIR
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
        tools, handlers = registry.assemble_tool_pool(
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
