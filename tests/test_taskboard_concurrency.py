"""TaskBoard 跨进程并发测试。

回归目标：AgentWorker 是 ``spawn`` 出来的独立进程，每个进程各自 new 一个
TaskBoard。早期实现只用 ``threading.Lock`` 保护认领，那把锁对独立进程
毫无约束 —— 两个 worker 会同时读到同一个 PENDING 任务、各自追加一条
CLAIMED，于是同一任务被执行两次、同一份工作区被写两遍。

所以这里必须用**真进程**测，线程测不出问题。
"""

import multiprocessing as mp
from pathlib import Path

import pytest

from myharness.teams.taskboard import TaskBoard, TaskStatus


def _claim_in_subprocess(board_path: str, barrier, out_path: str, agent_id: str) -> None:
    """子进程：等所有进程就绪后抢一个任务，结果写进各自的文件。

    用文件而不是 Queue 回传：Queue 在 join 之后再读容易卡住，
    而这里只需要传一个字符串。
    """
    from myharness.teams.taskboard import TaskBoard as TB

    board = TB(Path(board_path))
    barrier.wait()
    task = board.claim_task(agent_id)
    Path(out_path).write_text(task.task_id if task else "", encoding="utf-8")


def _create_in_subprocess(board_path: str, barrier, agent_id: str, count: int) -> None:
    """子进程：并发创建任务，用来验证 append 不会交错写坏。"""
    from myharness.teams.taskboard import TaskBoard as TB

    board = TB(Path(board_path))
    barrier.wait()
    for i in range(count):
        board.create_task(f"{agent_id}-{i}", "concurrent create")


@pytest.fixture
def spawn_ctx():
    try:
        return mp.get_context("spawn")
    except ValueError:  # pragma: no cover - 平台兜底
        pytest.skip("spawn context unavailable")


def _run(procs: list, join_timeout: float = 120.0) -> None:
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=join_timeout)
        assert p.exitcode == 0, f"子进程异常退出: {p.exitcode}"


class TestClaimRace:
    """同一任务只能被一个 worker 认领。"""

    def test_concurrent_claim_only_one_wins(self, tmp_path, spawn_ctx):
        """4 个进程同时抢一个任务，只有一个能拿到。"""
        board_path = tmp_path / "taskboard.jsonl"
        board = TaskBoard(board_path)
        task_id = board.create_task("并发认领", "只能被一个 worker 执行")

        n = 4
        barrier = spawn_ctx.Barrier(n)
        outs = [tmp_path / f"claim_{i}.txt" for i in range(n)]
        procs = [
            spawn_ctx.Process(
                target=_claim_in_subprocess,
                args=(str(board_path), barrier, str(outs[i]), f"agent_{i}"),
            )
            for i in range(n)
        ]

        _run(procs)

        claimed = [out.read_text(encoding="utf-8") for out in outs]
        winners = [c for c in claimed if c]
        assert winners == [task_id], f"应只有一个赢家，实际: {claimed}"

    def test_repeated_races_stay_exclusive(self, tmp_path, spawn_ctx):
        """连抢 3 轮，每轮只能有一个赢家（排除偶发通过）。"""
        board_path = tmp_path / "taskboard.jsonl"
        board = TaskBoard(board_path)

        for round_no in range(3):
            task_id = board.create_task(f"第 {round_no} 轮", "并发认领")

            n = 3
            barrier = spawn_ctx.Barrier(n)
            outs = [tmp_path / f"r{round_no}_{i}.txt" for i in range(n)]
            procs = [
                spawn_ctx.Process(
                    target=_claim_in_subprocess,
                    args=(str(board_path), barrier, str(outs[i]), f"agent_{i}"),
                )
                for i in range(n)
            ]

            _run(procs)

            winners = [out.read_text(encoding="utf-8") for out in outs]
            winners = [w for w in winners if w]
            assert winners == [task_id], f"第 {round_no} 轮出现重复认领: {winners}"

    def test_board_records_single_claim(self, tmp_path, spawn_ctx):
        """认领写入任务板的 claimed_by 只能是赢家那一个。"""
        board_path = tmp_path / "taskboard.jsonl"
        board = TaskBoard(board_path)
        task_id = board.create_task("唯一认领者", "检查落盘状态")

        n = 3
        barrier = spawn_ctx.Barrier(n)
        outs = [tmp_path / f"w_{i}.txt" for i in range(n)]
        procs = [
            spawn_ctx.Process(
                target=_claim_in_subprocess,
                args=(str(board_path), barrier, str(outs[i]), f"agent_{i}"),
            )
            for i in range(n)
        ]
        _run(procs)

        task = TaskBoard(board_path).get_task(task_id)
        assert task is not None
        assert task.status == TaskStatus.CLAIMED
        assert task.claimed_by in {"agent_0", "agent_1", "agent_2"}

        winner = next(
            agent for agent in ("agent_0", "agent_1", "agent_2")
            if task.claimed_by == agent
        )
        # 赢家的输出必须是这个 task_id，且只有它一个
        got = [o.read_text(encoding="utf-8") for o in outs]
        assert got.count(task_id) == 1
        assert f"agent_{got.index(task_id)}" == winner


class TestAppendRace:
    """并发追加不能写坏 JSON Lines。"""

    def test_concurrent_create_keeps_all_tasks(self, tmp_path, spawn_ctx):
        n, per_agent = 3, 4
        board_path = tmp_path / "taskboard.jsonl"
        TaskBoard(board_path)  # 先把文件建出来

        barrier = spawn_ctx.Barrier(n)
        procs = [
            spawn_ctx.Process(
                target=_create_in_subprocess,
                args=(str(board_path), barrier, f"agent_{i}", per_agent),
            )
            for i in range(n)
        ]
        _run(procs)

        tasks = TaskBoard(board_path).list_tasks()
        assert len(tasks) == n * per_agent
        assert all(t.status == TaskStatus.PENDING for t in tasks)
        assert len({t.task_id for t in tasks}) == n * per_agent
