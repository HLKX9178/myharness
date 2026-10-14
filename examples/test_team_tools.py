#!/usr/bin/env python3
"""测试 Teams 工具集成。

验证：
1. spawn_team() - 启动团队
2. create_task() - 创建任务
3. list_tasks() - 列出任务
4. get_task_result() - 获取结果
5. stop_team() - 停止团队
"""

import sys
import time
from pathlib import Path

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from myharness.tools import (
    spawn_team,
    stop_team,
    create_task,
    list_tasks,
    get_task_result,
    send_team_message,
)


def main():
    print("=" * 70)
    print("Teams Tools Integration Test")
    print("=" * 70)

    workspace = Path(__file__).parent / "team_test_workspace"
    workspace.mkdir(exist_ok=True)

    # Test 1: Spawn team
    print("\n[1] Spawning team with 3 agents...")
    result = spawn_team(num_agents=3, workspace=workspace)
    print(f"    Result: {result}")

    if "Error" in result:
        print("\n[!] Failed to spawn team. Exiting.")
        return 1

    time.sleep(2)  # 让 agents 启动

    # Test 2: Create tasks
    print("\n[2] Creating tasks...")

    task1_result = create_task(
        title="Task 1: Calculate sum",
        description="Calculate the sum of numbers 1 to 100"
    )
    print(f"    Task 1: {task1_result}")

    task2_result = create_task(
        title="Task 2: List files",
        description="List all Python files in the workspace"
    )
    print(f"    Task 2: {task2_result}")

    task3_result = create_task(
        title="Task 3: Write file",
        description="Create a file named test.txt with content 'Hello Team!'"
    )
    print(f"    Task 3: {task3_result}")

    # Extract task IDs (format: "Task created: <id>. ...")
    task_ids = []
    for result in [task1_result, task2_result, task3_result]:
        if "Task created:" in result:
            task_id = result.split("Task created:")[1].split(".")[0].strip()
            task_ids.append(task_id)

    # Test 3: List tasks
    print("\n[3] Listing tasks...")
    result = list_tasks()
    print(f"    {result}")

    # Test 4: Wait and check results
    print("\n[4] Waiting for tasks to complete (10 seconds)...")
    time.sleep(10)

    print("\n[5] Checking task results...")
    for task_id in task_ids:
        result = get_task_result(task_id)
        print(f"    Task {task_id}:")
        print(f"    {result}\n")

    # Test 5: List tasks again
    print("\n[6] Final task status...")
    result = list_tasks()
    print(f"    {result}")

    # Test 6: Stop team
    print("\n[7] Stopping team...")
    result = stop_team()
    print(f"    Result: {result}")

    print("\n" + "=" * 70)
    print("Test Complete!")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n[!] Interrupted. Cleaning up...")
        stop_team()
        sys.exit(1)
    except Exception as e:
        print(f"\n[!] Error: {e}")
        import traceback
        traceback.print_exc()
        stop_team()
        sys.exit(1)
