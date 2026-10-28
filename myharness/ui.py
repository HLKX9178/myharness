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


class StreamPrinter:
    """把模型输出边收边打到终端。

    作为 `on_text` 回调传给 `agent_loop`。只在第一段文字到达时才打印
    「BOT >>」前缀 —— 模型可能整轮只调工具、一句话不说，提前打印会留下
    一个空标题。

    一条用户消息往往对应多次模型调用（中间夹着工具执行）。`agent_loop`
    在每次调用前后触发 `begin_message()` / `end_message()`，于是每条 assistant
    消息各自带前缀、各自收尾，不会出现「正文接着工具日志往下写」的观感。
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.started = False          # 本轮是否输出过正文（CLI 据此决定要不要补打最终回复）
        self._awaiting_prefix = True  # 当前这条 assistant 消息还没打前缀
        self._mid_line = False        # 当前行有未结束的正文

    def begin_message(self) -> None:
        """新的 assistant 消息开始：下一个分片前重新打前缀。"""
        self._awaiting_prefix = True

    def __call__(self, text: str) -> None:
        if not self.enabled or not text:
            return
        if self._awaiting_prefix:
            # 颜色只设一次：逐片包 ANSI 码会把输出撑大几倍
            print(f"{Color.GREEN}{Color.BOLD}BOT >> {Color.RESET}{Color.GREEN}", end="", flush=True)
            self._awaiting_prefix = False
            self.started = True
        print(text, end="", flush=True)
        self._mid_line = not text.endswith("\n")

    def end_message(self) -> None:
        """这条消息说完：复位颜色并换行，让后续日志从行首开始。"""
        if not self._mid_line:
            return
        print(Color.RESET, flush=True)
        self._mid_line = False
        self._awaiting_prefix = True
