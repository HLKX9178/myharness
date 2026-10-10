"""状态持久化与恢复。

职责：
1. 定义任务状态数据结构
2. 保存任务执行状态到磁盘
3. 进程重启后恢复任务状态
"""

import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class TaskState:
    """任务执行状态（用于持久化）。"""
    task_id: str
    name: str
    status: str  # "pending" | "running" | "completed" | "failed"
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    checkpoint: dict[str, Any] | None = None  # 任务断点（幂等恢复）
    result: Any | None = None
    error: str | None = None


def save_state(state_file: Path, states: dict[str, TaskState]) -> None:
    """
    保存任务状态到磁盘。

    Args:
        state_file: 状态文件路径
        states: {task_id: TaskState} 映射
    """
    state_file.parent.mkdir(parents=True, exist_ok=True)
    data = {
        task_id: asdict(state)
        for task_id, state in states.items()
    }
    with state_file.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state(state_file: Path) -> dict[str, TaskState]:
    """
    从磁盘加载任务状态。

    Args:
        state_file: 状态文件路径

    Returns:
        {task_id: TaskState} 映射
    """
    if not state_file.exists():
        return {}

    try:
        with state_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {
            task_id: TaskState(**state_data)
            for task_id, state_data in data.items()
        }
    except Exception as exc:
        print(f"[persistence] Failed to load state: {exc}")
        return {}


def checkpoint_task(
    state_file: Path,
    task_id: str,
    checkpoint: dict[str, Any],
) -> None:
    """
    保存任务断点（幂等重试用）。

    Args:
        state_file: 状态文件路径
        task_id: 任务 ID
        checkpoint: 断点数据（如：已处理的文件列表、进度）
    """
    states = load_state(state_file)
    if task_id not in states:
        print(f"[persistence] Task {task_id} not found, cannot checkpoint")
        return

    states[task_id].checkpoint = checkpoint
    save_state(state_file, states)


def get_checkpoint(state_file: Path, task_id: str) -> dict[str, Any] | None:
    """
    获取任务断点。

    Args:
        state_file: 状态文件路径
        task_id: 任务 ID

    Returns:
        断点数据或 None
    """
    states = load_state(state_file)
    if task_id not in states:
        return None
    return states[task_id].checkpoint
