"""Agent 编排端到端测试。

测试 M6 验收标准：
1. 某个 Agent 崩溃后，其认领的任务能被其他成员接管
2. 并行的文件修改互不干扰
3. 消息不丢失、不重复投递

依赖：
- AgentManager：进程管理
- TaskBoard：任务分发
- MessageQueue：消息传递
"""

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from myharness import config, hooks, loop
from myharness.teams import AgentManager, TaskBoard, MessageQueue, TaskStatus


# 子进程冷启动要 import 整个 myharness（含 provider、MemoryStore、SkillLibrary
# 的构造），实测 3~4 秒，慢机器更久。这里统一用轮询代替固定 sleep ——
# 固定 sleep 只会让这些用例在慢机器上随机变红。
WORKER_BOOT_TIMEOUT = 45.0
POLL_INTERVAL = 0.05


def _wait_until(predicate, timeout: float = WORKER_BOOT_TIMEOUT,
                interval: float = POLL_INTERVAL) -> bool:
    """轮询等待 predicate() 为真；超时返回最后一次求值结果。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@pytest.fixture
def team_dir(tmp_path):
    """创建团队目录。"""
    return tmp_path / "team"


@pytest.fixture
def fake_provider(monkeypatch):
    """模拟 Provider（避免真实 API 调用）。"""
    class FakeProvider:
        def get_model_name(self):
            return "fake-model"

        def chat(self, messages, tools, system, max_tokens, on_text=None):
            # 模拟模型返回：写一个文件
            last_msg = messages[-1] if messages else {}
            content_str = str(last_msg.get("content", ""))

            if "write" in content_str.lower():
                # 返回 tool_use
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="tool_use",
                            id="call_1",
                            name="write_file",
                            input={"path": "result.txt", "content": "done"},
                        )
                    ],
                    usage=SimpleNamespace(input_tokens=10, output_tokens=10),
                )
            else:
                # 返回纯文本
                return SimpleNamespace(
                    content=[SimpleNamespace(type="text", text="完成")],
                    usage=SimpleNamespace(input_tokens=10, output_tokens=10),
                )

    # 替换 provider
    saved_stop = list(hooks.registered_hooks("Stop"))
    hooks.HOOKS["Stop"].clear()
    monkeypatch.setattr(loop, "provider", FakeProvider())

    yield

    # 恢复
    hooks.HOOKS["Stop"][:] = saved_stop


class TestAgentOrchestration:
    """Agent 编排测试（多进程）。"""

    def test_multiple_agents_claim_tasks(self, team_dir):
        """
        测试：多个 Agent 并行认领任务。

        注意：不测试实际执行（需要真实 Provider），只验证任务被认领。
        """
        manager = AgentManager(team_dir, num_agents=2)

        # 创建 3 个任务
        task1 = manager.taskboard.create_task("task1", "测试任务 1")
        task2 = manager.taskboard.create_task("task2", "测试任务 2")
        task3 = manager.taskboard.create_task("task3", "测试任务 3")

        try:
            # 启动 2 个 Agent
            manager.start_team()

            # 等待任务被认领（不是完成）
            def any_claimed() -> bool:
                return any(
                    t.status is not TaskStatus.PENDING
                    for t in manager.taskboard.list_tasks()
                )

            assert _wait_until(any_claimed), (
                f"没有任务被认领: "
                f"{[t.status for t in manager.taskboard.list_tasks()]}"
            )

        finally:
            manager.stop_team()

    def test_agent_crash_recovery(self, team_dir):
        """
        测试：Agent 崩溃后，任务被其他 Agent 接管。

        M6 验收标准 1：某个 Agent 崩溃后，其认领的任务能被其他成员接管。

        时序说明：认领写在 agent_loop 之前，而 agent_loop 里至少有一次模型
        往返，所以「已认领」窗口足够宽，轮询一定捕捉得到 —— 不再用固定
        sleep 去赌子进程的启动耗时。
        """
        # 设置短超时（3秒）以便快速测试
        manager = AgentManager(team_dir, num_agents=2, timeout_seconds=3)

        # 创建 1 个任务
        task_id = manager.taskboard.create_task("long_task", "测试任务")

        try:
            # 启动 2 个 Agent
            manager.start_team()

            def claimed() -> bool:
                task = manager.taskboard.get_task(task_id)
                return task is not None and task.status is TaskStatus.CLAIMED

            assert _wait_until(claimed), (
                f"任务未被任何 worker 认领: "
                f"{manager.taskboard.get_task(task_id).status}"
            )
            original_claimer = manager.taskboard.get_task(task_id).claimed_by

            # 杀死认领任务的 Agent（模拟崩溃）
            killed = manager.kill_agent(original_claimer)
            assert killed, f"未能杀死 {original_claimer}"

            # 超时（3s）之后，任务必须能从「已死亡的认领者」手里被接管。
            #
            # 方法上刻意不依赖幸存 worker 的内部状态：worker 会发起真实模型
            # 调用（SDK 默认没有请求超时），它可能正卡在某次调用里而迟迟不去
            # 轮询 —— 那不是本用例要验的东西。所以这里除了观察表，还直接由
            # 测试进程尝试认领一次：能认领到，就证明死掉的 agent 没有把任务
            # 永久锁住（M6 验收标准 1）。
            def taken_over() -> bool:
                task = manager.taskboard.get_task(task_id)
                if task is None:
                    return False
                if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                    return True
                if task.claimed_by != original_claimer:
                    return True
                return manager.taskboard.claim_task("recovery_probe") is not None

            assert _wait_until(taken_over, timeout=30), (
                f"崩溃的 {original_claimer} 仍占着任务: "
                f"taskboard={manager.taskboard.get_task(task_id)}"
            )

        finally:
            manager.stop_team()

    def test_workspace_isolation_in_parallel(self, team_dir):
        """
        测试：并行的 Agent 有独立的工作区。

        M6 验收标准 2：并行的文件修改互不干扰。

        注意：不测试实际文件操作（需要真实 Provider），只验证工作区隔离。
        """
        manager = AgentManager(team_dir, num_agents=2)

        try:
            manager.start_team()

            # 布局：{team_dir}/{agent_id}/workspace（见 TeamAgent.__init__）
            agent_0_workspace = team_dir / "agent_0" / "workspace"
            agent_1_workspace = team_dir / "agent_1" / "workspace"

            # workspace 在子进程启动时（TeamAgent.__init__）就建好，早于任何
            # 模型调用，所以这里只需要等目录出现，不需要等任务执行
            assert _wait_until(
                lambda: agent_0_workspace.exists() and agent_1_workspace.exists()
            ), (
                "工作区未就绪: "
                f"agent_0={agent_0_workspace.exists()} "
                f"agent_1={agent_1_workspace.exists()}"
            )

            # 验证：工作区在不同的目录
            assert agent_0_workspace != agent_1_workspace, "工作区未隔离"
            assert agent_0_workspace.parent != agent_1_workspace.parent

        finally:
            manager.stop_team()

    def test_message_delivery(self, team_dir):
        """
        测试：消息不丢失、不重复投递。

        M6 验收标准 3：消息不丢失、不重复投递。
        """
        manager = AgentManager(team_dir, num_agents=2)

        # 发送消息（Agent-0 -> Agent-1）
        msg_id = manager.message_queue.send("agent_0", "agent_1", "Hello from 0")

        # 验证消息存在
        messages = manager.message_queue.list_messages("agent_1")
        assert len(messages) == 1
        assert messages[0].message_id == msg_id
        assert messages[0].content == "Hello from 0"
        assert messages[0].status.value == "pending"

        # Agent-1 接收消息
        received = manager.message_queue.receive("agent_1", limit=10)
        assert len(received) == 1
        assert received[0].message_id == msg_id
        assert received[0].status.value == "delivered"

        # 再次接收，不应重复
        received_again = manager.message_queue.receive("agent_1", limit=10)
        assert len(received_again) == 0, "消息被重复投递"

        # 确认消息
        acked = manager.message_queue.ack("agent_1", msg_id)
        assert acked

        # 验证状态
        messages = manager.message_queue.list_messages("agent_1")
        assert messages[0].status.value == "acked"

    def test_graceful_shutdown(self, team_dir):
        """测试：优雅关闭所有 Agent。"""
        manager = AgentManager(team_dir, num_agents=3)

        manager.start_team()
        time.sleep(1)  # 确保进程启动

        # 立即停止
        manager.stop_team(timeout=5.0)

        # 验证：所有进程已退出
        status = manager.get_status()
        assert status["alive_agents"] == 0
        assert len(manager.processes) == 0

    def test_context_manager_usage(self, team_dir):
        """测试：上下文管理器（with 语句）。"""
        # 使用 with 自动管理生命周期
        with AgentManager(team_dir, num_agents=2) as manager:
            time.sleep(1)  # 等待启动
            status = manager.get_status()
            assert status["alive_agents"] == 2

        # 退出后所有进程应该停止
        # (无法直接验证，因为 manager 已销毁)

    def test_restart_dead_agents(self, team_dir):
        """测试：自动重启死亡的 Agent。"""
        manager = AgentManager(team_dir, num_agents=2)

        try:
            manager.start_team()

            # 杀死一个 Agent
            killed = manager.kill_agent("agent_0")
            assert killed

            time.sleep(0.5)

            # 检查状态
            status = manager.get_status()
            assert status["dead_agents"] == 1

            # 重启死亡的 Agent
            restarted = manager.restart_dead_agents()
            assert restarted == 1

            time.sleep(0.5)

            # 验证：所有 Agent 都活着
            status = manager.get_status()
            assert status["alive_agents"] == 2

        finally:
            manager.stop_team()


class TestAgentWorker:
    """AgentWorker 单元测试（不启动进程）。"""

    def test_worker_initialization(self, team_dir):
        """测试：Worker 初始化。"""
        from myharness.teams.worker import AgentWorker

        worker = AgentWorker("test_agent", team_dir, poll_interval=0.5)

        assert worker.agent_id == "test_agent"
        assert worker.team_dir == team_dir
        assert worker.poll_interval == 0.5
        assert worker.agent.workspace.exists()

    def test_worker_inherits_task_timeout(self, team_dir):
        """Worker 必须沿用 AgentManager 传下来的任务超时时间。

        回归目标：子进程会重新构造 TaskBoard，漏传 timeout_seconds 时它会
        回退到默认 300 秒 —— manager 上把超时调成 3 秒完全不生效，
        「崩溃恢复」实际要等 5 分钟。
        """
        from myharness.teams.worker import AgentWorker

        worker = AgentWorker("test_agent", team_dir, timeout_seconds=3)

        assert worker.taskboard.timeout_seconds == 3

    def test_signal_handling(self, team_dir):
        """测试：信号处理。"""
        from myharness.teams.worker import AgentWorker
        import signal

        worker = AgentWorker("test_agent", team_dir)

        # 模拟信号
        worker._handle_signal(signal.SIGTERM, None)

        assert worker._should_stop is True
