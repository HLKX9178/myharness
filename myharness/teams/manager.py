"""Agent 进程管理器。

核心功能：
1. 启动多个 Agent 进程
2. 监控进程状态
3. 优雅关闭所有 Agent
4. 崩溃检测与恢复

设计：
- 使用 multiprocessing.Process 创建独立进程
- 每个 Agent 有独立的工作目录（workspace）
- 通过共享文件系统通信（TaskBoard / MessageQueue）
"""

import multiprocessing
import time
from pathlib import Path
from typing import Any

from myharness.teams.messaging import MessageQueue
from myharness.teams.taskboard import TaskBoard
from myharness.teams.worker import run_worker


class AgentManager:
    """
    管理多个 Agent 进程的生命周期。

    职责：
    1. 创建共享基础设施（TaskBoard / MessageQueue）
    2. 启动 N 个 Agent 进程
    3. 监控进程状态
    4. 优雅关闭
    """

    def __init__(
        self,
        team_dir: Path,
        num_agents: int = 2,
        poll_interval: float = 1.0,
        timeout_seconds: int = 300,
    ):
        """
        初始化 Agent 管理器。

        Args:
            team_dir: 团队目录
            num_agents: Agent 数量
            poll_interval: 轮询间隔（秒）
            timeout_seconds: 任务超时时间（秒）
        """
        self.team_dir = Path(team_dir)
        self.num_agents = num_agents
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds

        # Agent 进程列表
        self.processes: dict[str, multiprocessing.Process] = {}

        # 确保团队目录存在
        self.team_dir.mkdir(parents=True, exist_ok=True)

        # 创建共享基础设施
        self.taskboard = TaskBoard(
            self.team_dir / "taskboard.jsonl",
            timeout_seconds=timeout_seconds,
        )
        self.message_queue = MessageQueue(self.team_dir / "messages")

    def spawn_agent(self, agent_id: str) -> multiprocessing.Process:
        """
        创建并启动一个 Agent 进程。

        Args:
            agent_id: Agent ID

        Returns:
            Process 对象
        """
        process = multiprocessing.Process(
            target=run_worker,
            args=(agent_id, self.team_dir, self.poll_interval, self.timeout_seconds),
            name=agent_id,
        )
        process.start()
        self.processes[agent_id] = process
        return process

    def start_team(self) -> None:
        """启动所有 Agent 进程。"""
        print(f"启动 {self.num_agents} 个 Agent...")
        for i in range(self.num_agents):
            agent_id = f"agent_{i}"
            self.spawn_agent(agent_id)
            print(f"  - {agent_id} 已启动 (PID: {self.processes[agent_id].pid})")

    def stop_team(self, timeout: float = 5.0) -> None:
        """
        停止所有 Agent 进程（优雅关闭）。

        Args:
            timeout: 等待进程退出的超时时间（秒）
        """
        print(f"停止 {len(self.processes)} 个 Agent...")

        for agent_id, process in self.processes.items():
            if process.is_alive():
                print(f"  - 停止 {agent_id} (PID: {process.pid})")
                process.terminate()  # 发送 SIGTERM

        # 等待所有进程退出
        start_time = time.time()
        for agent_id, process in self.processes.items():
            remaining = timeout - (time.time() - start_time)
            if remaining <= 0:
                break

            if process.is_alive():
                process.join(timeout=remaining)

            if process.is_alive():
                print(f"  - {agent_id} 未在超时内退出，强制终止")
                process.kill()
                process.join()

        self.processes.clear()
        print("所有 Agent 已停止")

    def get_status(self) -> dict[str, Any]:
        """
        获取所有 Agent 的状态。

        Returns:
            状态字典
        """
        alive_count = sum(1 for p in self.processes.values() if p.is_alive())

        return {
            "total_agents": len(self.processes),
            "alive_agents": alive_count,
            "dead_agents": len(self.processes) - alive_count,
            "agents": {
                agent_id: {
                    "pid": process.pid,
                    "alive": process.is_alive(),
                    "exitcode": process.exitcode,
                }
                for agent_id, process in self.processes.items()
            },
        }

    def kill_agent(self, agent_id: str) -> bool:
        """
        杀死指定的 Agent 进程（用于测试崩溃恢复）。

        Args:
            agent_id: Agent ID

        Returns:
            是否成功杀死
        """
        process = self.processes.get(agent_id)
        if not process or not process.is_alive():
            return False

        print(f"杀死 Agent {agent_id} (PID: {process.pid})")
        process.kill()
        process.join()
        return True

    def wait_for_completion(
        self,
        check_interval: float = 1.0,
        timeout: float | None = None,
    ) -> bool:
        """
        等待所有任务完成。

        Args:
            check_interval: 检查间隔（秒）
            timeout: 超时时间（秒，None 表示无限等待）

        Returns:
            是否在超时内完成（True）或超时（False）
        """
        start_time = time.time()

        while True:
            # 检查是否有待处理或执行中的任务
            pending = self.taskboard.list_tasks(status=None)
            incomplete = [
                t for t in pending
                if t.status.value in ["pending", "claimed"]
            ]

            if not incomplete:
                print("所有任务已完成")
                return True

            # 检查超时
            if timeout and (time.time() - start_time) > timeout:
                print(f"等待超时，还有 {len(incomplete)} 个任务未完成")
                return False

            time.sleep(check_interval)

    def restart_dead_agents(self) -> int:
        """
        重启已死亡的 Agent 进程。

        Returns:
            重启的 Agent 数量
        """
        restarted = 0

        for agent_id, process in list(self.processes.items()):
            if not process.is_alive():
                print(f"检测到 {agent_id} 已停止，重启...")
                new_process = self.spawn_agent(agent_id)
                print(f"  - {agent_id} 已重启 (PID: {new_process.pid})")
                restarted += 1

        return restarted

    def monitor_and_restart(
        self,
        check_interval: float = 5.0,
        duration: float | None = None,
    ) -> None:
        """
        监控 Agent 状态并自动重启死亡的 Agent（守护模式）。

        Args:
            check_interval: 检查间隔（秒）
            duration: 运行时长（秒，None 表示无限运行）
        """
        print(f"启动监控模式（检查间隔 {check_interval}s）")
        start_time = time.time()

        try:
            while True:
                # 检查并重启死亡的 Agent
                restarted = self.restart_dead_agents()
                if restarted > 0:
                    print(f"已重启 {restarted} 个 Agent")

                # 检查是否到达运行时长
                if duration and (time.time() - start_time) > duration:
                    print("监控时长结束")
                    break

                time.sleep(check_interval)

        except KeyboardInterrupt:
            print("\n监控模式被中断")

    def __enter__(self):
        """上下文管理器：启动团队。"""
        self.start_team()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器：停止团队。"""
        self.stop_team()
