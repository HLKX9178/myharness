"""Provider 基类：定义统一的模型调用接口与**错误契约**。

模型调用的三条约定，都落在这个文件里：

1. **接口统一** —— 所有 provider 实现 `chat()` / `get_model_name()`。
2. **错误归一** —— SDK 异常必须在 provider 层被翻译成 `ProviderError`，
   不允许泄漏给 `agent_loop`。否则一次 403 或超时就会把异常抛穿 CLI，
   连尚未保存的会话一起丢掉（对应 AGENTS.md 契约 4：错误是数据，不是异常）。
3. **重试归属** —— 重试是 providers 层的职责（见 AGENTS.md 模块职责表）。
   只有「重试可能成功」的错误才重试；401/403/404 这类永久性拒绝重试
   只是白白让用户多等几秒。既然重试归本层，SDK 自带的重试就必须关掉
   （`max_retries=0`），否则两层叠加，最坏一次调用会发 9 次真实请求。
4. **超时自己定** —— 不依赖 SDK 默认的 600s。见下方常量说明。

重试通知目前直接打印，没有走 hook：hooks 只认四个固定事件（见 hooks.py），
为一次重试新增事件类型不划算 —— 横切策略归 hook，而重试是 provider 的
实现细节。
"""

import json
import os
import time
from abc import ABC, abstractmethod
from typing import Any, Callable

from myharness.ui import Color

# 全局注册表：provider名 → Provider类
PROVIDER_REGISTRY: dict[str, type["BaseProvider"]] = {}

# ---- 重试策略默认值 ----

# 首次之外的额外尝试次数，即最多发起 max_retries + 1 次请求。
DEFAULT_MAX_RETRIES = 2
# 指数退避基准（秒）：第 n 次重试等待 retry_base_delay * 2^(n-1)。
DEFAULT_RETRY_BASE_DELAY = 0.5
# 单次退避上限（秒），避免服务端给出夸张的 retry-after 把 CLI 挂住。
MAX_RETRY_DELAY = 8.0
# 5xx 之外仍值得重试的状态码：请求超时 / 冲突 / 过早 / 限流。
RETRYABLE_STATUS = frozenset({408, 409, 425, 429})

# ---- 请求超时（单次 HTTP 请求）----
#
# SDK 的默认超时是 600s。本层最多尝试 max_retries + 1 次，于是「一次卡死」
# 的实际代价是 600 × 3 = 30 分钟 —— 在多 Agent 场景下表现为该 worker 静默
# 停止轮询（任务被永久占住），而外层只看得到「什么都没发生」。
#
# 因此显式设成 120s：足够覆盖正常的生成长度，又把最坏情况压到 6 分钟以内。
# 连接超时单独收紧到 10s —— 「连不上」几秒内就有结论，不该跟着读超时干等。
DEFAULT_REQUEST_TIMEOUT = 120.0
DEFAULT_CONNECT_TIMEOUT = 10.0


def resolve_request_timeout(override: float | None = None) -> float:
    """决定单次请求的超时秒数。

    优先级：显式参数 > 环境变量 `REQUEST_TIMEOUT_SECONDS` > 默认值。

    非法值直接抛 ValueError，不静默回退到默认值 —— 与 config._env_int 同一
    约定：把「我明明设了却没生效」变成一条能定位的错误，比悄悄用默认值好。
    """
    if override is not None:
        value = float(override)
    else:
        raw = os.getenv("REQUEST_TIMEOUT_SECONDS")
        if raw is None or not raw.strip():
            return DEFAULT_REQUEST_TIMEOUT
        try:
            value = float(raw.strip())
        except ValueError:
            raise ValueError(
                f"REQUEST_TIMEOUT_SECONDS must be a number, got {raw!r}. "
                f"Fix REQUEST_TIMEOUT_SECONDS in .env "
                f"(for example REQUEST_TIMEOUT_SECONDS={DEFAULT_REQUEST_TIMEOUT:g})."
            ) from None

    if value <= 0:
        raise ValueError(
            f"REQUEST_TIMEOUT_SECONDS must be > 0, got {value}. Fix it in .env."
        )
    return value


def build_http_timeout(request_timeout: float, timeout_cls: Callable[..., Any]) -> Any:
    """构造 SDK 的超时对象：连接收紧、读写按 request_timeout。

    超时的**形状**只在这里定义一次，两个 provider 共用，避免两边各写一份
    （两份形状迟早有一份忘记更新）。

    `timeout_cls` 由各 provider 从自己的 SDK 传入（`openai.Timeout` /
    `anthropic.Timeout`），而不是本模块直接 import —— SDK 的 HTTP 实现包
    （此环境里是 `httpx2`）属于它的内部细节，不是我们要声明的直接依赖。
    """
    connect = min(DEFAULT_CONNECT_TIMEOUT, request_timeout)
    return timeout_cls(
        connect=connect,
        read=request_timeout,
        write=request_timeout,
        pool=connect,
    )


class ProviderError(Exception):
    """规范化后的模型调用错误。

    属性：
        message      一行诊断，可直接展示给用户（不含堆栈）
        retryable    重试是否可能成功
        status_code  HTTP 状态码；连接失败等场景为 None
        code         服务端业务错误码（如 AccessDenied.Unpurchased）
        hint         针对该错误的修复建议；无建议时为 None
        retry_after  服务端要求的等待秒数（来自 retry-after 响应头）
        attempts     实际发起过的请求次数（含重试）
    """

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        status_code: int | None = None,
        code: str | None = None,
        hint: str | None = None,
        retry_after: float | None = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.status_code = status_code
        self.code = code
        self.hint = hint
        self.retry_after = retry_after
        self.attempts = attempts


def is_retryable_status(status_code: int) -> bool:
    """该 HTTP 状态码是否值得重试。"""
    return status_code >= 500 or status_code in RETRYABLE_STATUS


def hint_for_status(status_code: int | None) -> str | None:
    """按 HTTP 状态码给出**可执行**的修复建议。

    OpenAI 系与 Anthropic 系兼容端点在下列状态码上的语义一致，所以共用
    一份实现，而不是每个 provider 各写一份字符串 —— 两份字符串迟早会有
    一份忘记更新。
    """
    if status_code == 401:
        return "API Key 被拒绝（401）。检查 .env 中的密钥是否有效、是否已过期。"
    if status_code == 403:
        return (
            "账号无权调用该模型（403）。常见原因是模型未开通/未购买或额度用尽："
            "请到控制台开通该模型，或把 .env 的 MODEL_ID 换成有权限的模型。"
            "403 属于永久性拒绝，重试无用。"
        )
    if status_code == 404:
        return "模型或端点不存在（404）。检查 .env 的 MODEL_ID 与 base_url 是否拼写正确。"
    if status_code == 429:
        return "触发服务端限流（429）。已按退避重试；仍失败请降低调用频率或提升配额。"
    if status_code is not None and status_code >= 500:
        return "服务端错误。已按退避重试；持续失败请稍后再试。"
    return None


def extract_error_detail(body: Any) -> tuple[str | None, str]:
    """从错误响应体里取出 (业务错误码, 人类可读描述)。

    必须兼容两种形状，两者都真实出现过：

      {"error": {"code": ..., "message": ...}}   原始响应体
      {"code": ..., "message": ...}              SDK 已取出的 error 节点

    Args:
        body: SDK 异常上的 body 字段

    Returns:
        (错误码或 None, 描述文本；无法解析时退化为截断的原始内容)
    """
    node = body
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        node = body["error"]

    if isinstance(node, dict):
        raw_code = node.get("code") or node.get("type")
        code = str(raw_code) if raw_code else None
        message = node.get("message")
        if message:
            return code, str(message)
        return code, json.dumps(node, ensure_ascii=False)[:200]

    if node is None:
        return None, ""
    return None, str(node)[:200]


def parse_retry_after(exc: BaseException) -> float | None:
    """读取响应头 retry-after 中的秒数；缺失或非数字时返回 None。"""
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if not headers:
        return None
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(str(raw).strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


def cached_prompt_tokens(usage: Any) -> int:
    """从 usage 里取「命中前缀缓存的输入 token 数」；取不到返回 0。

    三家叫法不同，必须兼容：

        Anthropic           usage.cache_read_input_tokens
        OpenAI 兼容         usage.prompt_tokens_details.cached_tokens
        DeepSeek / 部分网关 usage.prompt_cache_hit_tokens

    **返回 0 不等于没命中**，也可能是端点压根不上报。所以调用方只能把它
    当作「命中了多少」的上界证据，不能反推「一定没命中」——判据见 loop 里
    的打印说明。
    """
    for attribute in ("cache_read_input_tokens", "prompt_cache_hit_tokens", "cached_tokens"):
        value = getattr(usage, attribute, None)
        if isinstance(value, int):
            return value

    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        value = getattr(details, "cached_tokens", None)
        if isinstance(value, int):
            return value
        if isinstance(details, dict):
            value = details.get("cached_tokens")
            if isinstance(value, int):
                return value
    return 0


class BaseProvider(ABC):
    """所有模型提供商的抽象基类。"""

    # 重试参数做成类属性，子类与测试可以整体改写
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_base_delay: float = DEFAULT_RETRY_BASE_DELAY

    @classmethod
    def register(cls, name: str):
        """
        装饰器：将 Provider 类注册到全局注册表。

        用法：
            @BaseProvider.register("anthropic")
            class AnthropicProvider(BaseProvider):
                ...
        """
        def wrapper(provider_class):
            PROVIDER_REGISTRY[name] = provider_class
            return provider_class
        return wrapper

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str,
        max_tokens: int,
        on_text: Callable[[str], None] | None = None,
    ) -> Any:
        """
        发起一次对话请求。

        Args:
            messages: 对话历史（OpenAI 格式）
            tools: 工具列表（OpenAI 格式）
            system: 系统提示词
            max_tokens: 最大生成 token 数
            on_text: 文本分片回调。None 表示不要流式；传入时实现应边收边调，
                但**返回值必须与不传时完全一致**（同一条消息、同一份 usage）——
                流式是传输细节，不能改变 Loop 看到的东西。

        Returns:
            标准化的响应对象（包含 content, usage 等字段）

        Raises:
            ProviderError: 调用失败。SDK 异常不得穿出本方法。

        **流式与重试的约定**：一旦通过 `on_text` 输出过内容，就不能再重试
        （重试会把同样的文字打印第二遍）。实现必须在首片之前允许重试，
        之后把任何失败转成 `retryable=False` 的 ProviderError。半截回复
        不得作为 assistant 消息进入历史 —— 抛异常即可，Loop 只在成功时落库。
        """
        pass

    @abstractmethod
    def get_model_name(self) -> str:
        """返回当前使用的模型名称。"""
        pass

    # ---- 错误归一与重试（子类只需实现 _classify_error）----

    @abstractmethod
    def _classify_error(self, exc: BaseException) -> ProviderError:
        """把本 provider 所用 SDK 的异常翻译成 ProviderError。

        子类必须覆盖所用 SDK 的全部异常分支；认不出来的一律归为**不可重试**，
        并保留原始异常类型名 —— 把编程错误（TypeError 之类）伪装成网络故障，
        只会让排查更难。
        """

    def _with_retry(self, operation: Callable[[], Any]) -> Any:
        """执行模型调用；可重试错误按退避重试，其余立即转成 ProviderError。

        Args:
            operation: 无参可调用对象，内部完成一次真实请求

        Returns:
            operation 的返回值

        Raises:
            ProviderError: 重试耗尽，或遇到不可重试的错误
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return operation()
            except Exception as exc:
                error = (
                    exc if isinstance(exc, ProviderError) else self._classify_error(exc)
                )

                if not error.retryable or attempt > self.max_retries:
                    error.attempts = attempt
                    # 保留原始异常链，日志里仍能看到 SDK 的完整信息
                    raise error from exc

                delay = self._retry_delay(error, attempt)
                print(
                    f"{Color.YELLOW}  [provider] {error.message[:100]} —— "
                    f"{delay:.1f}s 后重试（{attempt}/{self.max_retries}）{Color.RESET}"
                )
                time.sleep(delay)

    def _retry_delay(self, error: ProviderError, attempt: int) -> float:
        """退避时长：优先听服务端的 retry-after，否则 2 的幂退避。"""
        if error.retry_after is not None:
            return min(error.retry_after, MAX_RETRY_DELAY)
        return min(self.retry_base_delay * (2 ** (attempt - 1)), MAX_RETRY_DELAY)
