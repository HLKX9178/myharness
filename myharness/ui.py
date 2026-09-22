"""终端输出样式。"""


class Color:
    """ANSI 颜色码。Windows 10+ 终端默认支持。"""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"    # 用户输入
    GREEN = "\033[32m"   # 模型输出
    YELLOW = "\033[33m"  # 工具调用
    RED = "\033[31m"     # 错误
    GRAY = "\033[90m"    # 分隔线
