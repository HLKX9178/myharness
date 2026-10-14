"""大任务能力测试。

测试 Agent 是否能完成复杂的多步骤任务，例如：
- 创建一个完整的小网站
- 包含多个文件
- 需要规划和分步执行
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from myharness.loop import agent_loop


def test_website_creation():
    """测试：创建一个简单的个人网站。"""

    messages = [
        {
            "role": "user",
            "content": """请帮我创建一个简单但完整的个人主页网站，要求：

1. 包含 HTML、CSS、JavaScript 文件
2. 有导航栏、个人介绍、技能展示、项目展示、联系方式等板块
3. 响应式设计，在手机上也能正常显示
4. 有一些简单的交互效果（比如按钮悬停、平滑滚动）
5. 代码要有注释，结构清晰

请按照标准的 Web 开发流程来做，创建在 website/ 目录下。"""
        }
    ]

    # 准备工作目录
    workspace = Path.cwd() / "test_website"
    workspace.mkdir(exist_ok=True)

    print("=" * 70)
    print("大任务测试：创建个人网站")
    print("=" * 70)
    print(f"\n工作目录: {workspace}\n")

    # 执行任务
    try:
        agent_loop(messages, workspace=workspace)

        print("\n" + "=" * 70)
        print("任务执行完成！")
        print("=" * 70)

        # 检查生成的文件
        print("\n生成的文件：")
        for file in workspace.rglob("*"):
            if file.is_file():
                print(f"  - {file.relative_to(workspace)} ({file.stat().st_size} bytes)")

        # 统计执行情况
        assistant_turns = sum(1 for msg in messages if msg.get("role") == "assistant")
        tool_calls = 0
        for msg in messages:
            if msg.get("role") == "assistant":
                content = msg.get("content", [])
                if isinstance(content, list):
                    tool_calls += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use")

        print(f"\n执行统计：")
        print(f"  - Agent 轮次: {assistant_turns}")
        print(f"  - 工具调用次数: {tool_calls}")
        print(f"  - 消息总数: {len(messages)}")

        return True

    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_todo_app():
    """测试：创建一个 TODO 应用。"""

    messages = [
        {
            "role": "user",
            "content": """请帮我创建一个功能完整的 TODO 应用，要求：

1. 使用原生 HTML + CSS + JavaScript（不用框架）
2. 功能：添加任务、标记完成、删除任务、筛选（全部/未完成/已完成）
3. 数据持久化到 localStorage
4. 界面美观，有适当的动画效果
5. 响应式设计

创建在 todo-app/ 目录下。"""
        }
    ]

    workspace = Path.cwd() / "test_todo_app"
    workspace.mkdir(exist_ok=True)

    print("\n" + "=" * 70)
    print("大任务测试：创建 TODO 应用")
    print("=" * 70)
    print(f"\n工作目录: {workspace}\n")

    try:
        agent_loop(messages, workspace=workspace)

        print("\n" + "=" * 70)
        print("任务执行完成！")
        print("=" * 70)

        # 检查生成的文件
        print("\n生成的文件：")
        for file in workspace.rglob("*"):
            if file.is_file():
                print(f"  - {file.relative_to(workspace)} ({file.stat().st_size} bytes)")

        return True

    except Exception as e:
        print(f"\n错误: {e}")
        return False


if __name__ == "__main__":
    print("选择测试任务：")
    print("1. 创建个人网站")
    print("2. 创建 TODO 应用")

    choice = input("\n请选择 (1/2): ").strip()

    if choice == "1":
        success = test_website_creation()
    elif choice == "2":
        success = test_todo_app()
    else:
        print("无效选择")
        sys.exit(1)

    sys.exit(0 if success else 1)
