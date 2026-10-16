"""Agent 工作循环。

Agent 进程的核心逻辑：
1. 循环认领任务并执行
2. 检查并处理消息
3. 支持优雅退出

设计：
- 运行在独立进程中（由 AgentManager 启动）
- 通过共享文件系统通信（TaskBoard / MessageQueue）
- 支持信号退出（SIGTERM / SIGINT）
"""

import signal
import sys
import time
from pathlib import Path

# 强制使用 UTF-8 编码（避免 Windows GBK 编码问题）
import io
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

from myharness.teams.agent import TeamAgent
from myharness.teams.messaging import MessageQueue
from myharness.teams.taskboard import TaskBoard


class AgentWorker:
    """
    Agent 工作进程封装。

    在独立进程中运行，循环执行以下流程：
    1. 尝试认领任务
    2. 执行任务（agent_loop）
    3. 检查消息
    4. 检查退出信号
    """

    def __init__(
        self,
        agent_id: str,
        team_dir: Path,
        poll_interval: float = 1.0,
        timeout_seconds: int = 300,
    ):
        """
        初始化 Agent 工作进程。

        Args:
            agent_id: Agent ID
            team_dir: 团队目录
            poll_interval: 轮询间隔（秒）
            timeout_seconds: 任务超时时间（秒）。必须由 AgentManager 传进来 ——
                子进程会重新构造 TaskBoard，用默认值会让「崩溃恢复」退化成
                「等 300 秒」，配置在 manager 上改了也不生效。
        """
        self.agent_id = agent_id
        self.team_dir = team_dir
        self.poll_interval = poll_interval
        self._should_stop = False

        # 初始化共享基础设施
        self.taskboard = TaskBoard(
            team_dir / "taskboard.jsonl",
            timeout_seconds=timeout_seconds,
        )
        self.message_queue = MessageQueue(team_dir / "messages")

        # 创建 TeamAgent
        self.agent = TeamAgent(
            agent_id=agent_id,
            team_dir=team_dir,
            taskboard=self.taskboard,
            message_queue=self.message_queue,
        )

        # 注册信号处理（优雅退出）
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)

    def _handle_signal(self, signum, frame):
        """处理退出信号。"""
        print(f"[{self.agent_id}] 收到退出信号 {signum}，准备停止...")
        self._should_stop = True

    def run(self) -> None:
        """
        主工作循环。

        循环执行：
        1. 认领并执行任务
        2. 检查消息
        3. 休眠（避免 CPU 空转）
        """
        print(f"[{self.agent_id}] 启动，workspace={self.agent.workspace}")

        while not self._should_stop:
            try:
                # 1. 尝试认领并执行任务
                result = self.agent.claim_and_execute()

                if result["success"]:
                    task_id = result.get("task_id", "unknown")
                    print(f"[{self.agent_id}] 完成任务 {task_id}")
                else:
                    # 没有任务，检查消息
                    self._check_messages()

                    # 没有任务也没有消息，休眠一会儿
                    time.sleep(self.poll_interval)

            except KeyboardInterrupt:
                print(f"[{self.agent_id}] 收到键盘中断，退出")
                break
            except Exception as exc:
                print(f"[{self.agent_id}] 错误: {type(exc).__name__}: {exc}")
                time.sleep(self.poll_interval)

        print(f"[{self.agent_id}] 停止")

    def _check_messages(self) -> None:
        """检查并处理消息。"""
        messages = self.agent.receive_messages(limit=10)

        if messages:
            for msg in messages:
                print(
                    f"[{self.agent_id}] 收到来自 {msg.from_agent} 的消息: "
                    f"{str(msg.content)[:50]}"
                )
                # TODO: 实际项目中可以在这里处理消息
                # 例如：根据消息内容执行特定操作


def run_worker(
    agent_id: str,
    team_dir: Path,
    poll_interval: float = 1.0,
    timeout_seconds: int = 300,
) -> None:
    """
    启动 Agent 工作进程（进程入口函数）。

    Args:
        agent_id: Agent ID
        team_dir: 团队目录
        poll_interval: 轮询间隔（秒）
        timeout_seconds: 任务超时时间（秒），与 AgentManager 保持一致
    """
    worker = AgentWorker(agent_id, team_dir, poll_interval, timeout_seconds)
    worker.run()


if __name__ == "__main__":
    # 独立进程模式：从命令行参数读取配置
    if len(sys.argv) < 3:
        print("用法: python -m myharness.teams.worker <agent_id> <team_dir> [timeout_seconds]")
        sys.exit(1)

    agent_id = sys.argv[1]
    team_dir = Path(sys.argv[2])
    timeout_seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 300
    run_worker(agent_id, team_dir, timeout_seconds=timeout_seconds)
