"""多 Agent 团队协作示例。

演示如何使用 AgentManager 启动多个 Agent 进程，
让它们并行认领并执行任务。

用法：
    python examples/multi_agent_example.py
"""

import time
from pathlib import Path

from myharness.teams import AgentManager


def main():
    """运行多 Agent 团队示例。"""
    # 创建团队目录
    team_dir = Path("./team_workspace")
    team_dir.mkdir(exist_ok=True)

    print("=" * 60)
    print("多 Agent 团队协作示例")
    print("=" * 60)

    # 创建 AgentManager
    manager = AgentManager(
        team_dir=team_dir,
        num_agents=3,           # 启动 3 个 Agent
        poll_interval=1.0,       # 每秒轮询一次
        timeout_seconds=300,     # 任务超时 5 分钟
    )

    # 创建一些任务
    print("\n📝 创建任务...")
    tasks = [
        manager.taskboard.create_task(
            "重构代码",
            "提取 tools.py 中的重复逻辑"
        ),
        manager.taskboard.create_task(
            "编写测试",
            "为新增的 hooks 编写单元测试"
        ),
        manager.taskboard.create_task(
            "更新文档",
            "补充 AGENTS.md 中的示例"
        ),
        manager.taskboard.create_task(
            "代码审查",
            "检查 security.py 的权限边界"
        ),
    ]

    for task_id in tasks:
        print(f"  - {task_id}")

    # 启动 Agent 团队
    print("\n🚀 启动 3 个 Agent...")
    manager.start_team()

    try:
        # 监控进度
        print("\n⏳ 等待任务完成...")
        print("提示：按 Ctrl+C 可以提前停止\n")

        start_time = time.time()
        check_interval = 2.0

        while True:
            # 获取任务状态
            all_tasks = manager.taskboard.list_tasks()
            pending = [t for t in all_tasks if t.status.value == "pending"]
            claimed = [t for t in all_tasks if t.status.value == "claimed"]
            completed = [t for t in all_tasks if t.status.value == "completed"]
            failed = [t for t in all_tasks if t.status.value == "failed"]

            # 显示进度
            elapsed = time.time() - start_time
            print(
                f"[{elapsed:6.1f}s] "
                f"待认领: {len(pending)} | "
                f"执行中: {len(claimed)} | "
                f"已完成: {len(completed)} | "
                f"失败: {len(failed)}"
            )

            # 显示 Agent 状态
            status = manager.get_status()
            print(f"         Agent 状态: {status['alive_agents']}/{status['total_agents']} 存活")

            # 检查是否完成
            if not pending and not claimed:
                print("\n✅ 所有任务已完成！")
                break

            # 检查超时（演示用，实际可以去掉）
            if elapsed > 60:
                print("\n⏰ 演示超时，停止...")
                break

            time.sleep(check_interval)

        # 显示最终结果
        print("\n" + "=" * 60)
        print("📊 最终结果")
        print("=" * 60)

        for task in manager.taskboard.list_tasks():
            status_emoji = {
                "pending": "⏳",
                "claimed": "🔄",
                "completed": "✅",
                "failed": "❌",
            }.get(task.status.value, "❓")

            print(f"{status_emoji} {task.title}")
            print(f"   状态: {task.status.value}")
            if task.claimed_by:
                print(f"   负责人: {task.claimed_by}")
            if task.result:
                print(f"   结果: {task.result}")
            if task.error:
                print(f"   错误: {task.error}")
            print()

    except KeyboardInterrupt:
        print("\n\n⚠️  收到中断信号...")

    finally:
        # 优雅关闭所有 Agent
        print("\n🛑 停止所有 Agent...")
        manager.stop_team(timeout=5.0)
        print("✓ 所有 Agent 已停止")

    print("\n" + "=" * 60)
    print("提示：任务数据保存在:", team_dir / "taskboard.jsonl")
    print("      可以重新运行本脚本继续执行未完成的任务")
    print("=" * 60)


if __name__ == "__main__":
    main()
