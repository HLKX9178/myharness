"""标准评测任务集。

定义一组代表性任务用于评测 Agent 能力。

任务分类：
- basic: 基础工具使用
- reasoning: 推理与规划
- error_handling: 错误处理
- multi_step: 多步骤任务
"""

from myharness.eval import EvalTask


def has_text_output(messages: list[dict]) -> bool:
    """检查是否有文本输出。"""
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        return True
            elif isinstance(content, str) and content.strip():
                return True
    return False


def has_tool_call(messages: list[dict], tool_name: str) -> bool:
    """检查是否调用了指定工具。"""
    for msg in messages:
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        if block.get("name") == tool_name:
                            return True
    return False


def has_file_created(messages: list[dict], filename: str) -> bool:
    """检查是否创建了指定文件（通过 write_file 工具）。"""
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result = block.get("content", "")
                        if filename in str(result) or "success" in str(result).lower():
                            return True
    return False


def task_succeeded(messages: list[dict]) -> bool:
    """通用成功检查：有文本输出且无错误。"""
    if not has_text_output(messages):
        return False

    # 检查是否有错误
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        result = str(block.get("content", ""))
                        if result.startswith("Error:"):
                            return False
    return True


# ============================================================
# 基础任务集
# ============================================================

BASIC_TASKS = [
    EvalTask(
        task_id="basic_bash",
        description="Execute a simple bash command",
        user_input="List all Python files in the current directory",
        success_criteria=lambda msgs: has_tool_call(msgs, "bash") or has_tool_call(msgs, "glob_files"),
        category="basic",
        expected_cost_range=(500, 2000),
    ),
    EvalTask(
        task_id="basic_write_file",
        description="Create a file with specific content",
        user_input="Create a file called test.txt with the content 'Hello World'",
        success_criteria=lambda msgs: has_tool_call(msgs, "write_file") and task_succeeded(msgs),
        category="basic",
        expected_cost_range=(500, 2000),
    ),
    EvalTask(
        task_id="basic_read_file",
        description="Read a file that exists",
        user_input="Read the contents of README.md and tell me what this project is about",
        success_criteria=lambda msgs: has_tool_call(msgs, "read_file") and has_text_output(msgs),
        category="basic",
        expected_cost_range=(1000, 3000),
    ),
]


# ============================================================
# 推理任务集
# ============================================================

REASONING_TASKS = [
    EvalTask(
        task_id="reasoning_file_search",
        description="Find files matching a pattern",
        user_input="Find all markdown files in the project and list their names",
        success_criteria=lambda msgs: (has_tool_call(msgs, "glob_files") or has_tool_call(msgs, "bash"))
                                     and has_text_output(msgs),
        category="reasoning",
        expected_cost_range=(1000, 3000),
    ),
    EvalTask(
        task_id="reasoning_calculate",
        description="Perform calculation using bash",
        user_input="What is 123 * 456? Use bash to calculate it",
        success_criteria=lambda msgs: has_tool_call(msgs, "bash") and "56088" in str(msgs),
        category="reasoning",
        expected_cost_range=(500, 2000),
    ),
]


# ============================================================
# 错误处理任务集
# ============================================================

ERROR_HANDLING_TASKS = [
    EvalTask(
        task_id="error_file_not_found",
        description="Handle non-existent file gracefully",
        user_input="Read the file nonexistent.txt",
        success_criteria=lambda msgs: has_tool_call(msgs, "read_file") and has_text_output(msgs),
        category="error_handling",
        expected_cost_range=(500, 2000),
    ),
    EvalTask(
        task_id="error_invalid_command",
        description="Handle invalid bash command",
        user_input="Run the command 'invalidcommandxyz123'",
        success_criteria=lambda msgs: has_tool_call(msgs, "bash") and has_text_output(msgs),
        category="error_handling",
        expected_cost_range=(500, 2000),
    ),
]


# ============================================================
# 多步骤任务集
# ============================================================

MULTI_STEP_TASKS = [
    EvalTask(
        task_id="multi_create_and_verify",
        description="Create a file and verify its contents",
        user_input="Create a file named output.txt with 'Test content', then read it back to verify",
        success_criteria=lambda msgs: has_tool_call(msgs, "write_file")
                                     and has_tool_call(msgs, "read_file")
                                     and has_text_output(msgs),
        category="multi_step",
        expected_cost_range=(1500, 4000),
    ),
    EvalTask(
        task_id="multi_search_and_count",
        description="Search files and count results",
        user_input="Find all .py files in myharness/ directory and tell me how many there are",
        success_criteria=lambda msgs: (has_tool_call(msgs, "glob_files") or has_tool_call(msgs, "bash"))
                                     and has_text_output(msgs),
        category="multi_step",
        expected_cost_range=(1500, 4000),
    ),
]


# ============================================================
# 完整任务集
# ============================================================

ALL_TASKS = (
    BASIC_TASKS
    + REASONING_TASKS
    + ERROR_HANDLING_TASKS
    + MULTI_STEP_TASKS
)


def get_tasks_by_category(category: str) -> list[EvalTask]:
    """按分类获取任务。"""
    return [task for task in ALL_TASKS if task.category == category]
