"""配置加载与共享运行时依赖。

这里是路径、Provider 的**唯一来源**。其他模块一律从这里导入，
不得各自计算路径或自行创建客户端。
"""

import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv

from myharness.providers import PROVIDER_REGISTRY
from myharness.security import init_policy
from myharness.audit import init_audit_logger
from myharness.bootstrap import install_default_hooks
from myharness.memory import MemoryStore
from myharness.skills import BUILTIN_SKILL_DIR, SkillLibrary

# 仓库根目录：<repo>/myharness/config.py -> <repo>/myharness -> <repo>
REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env", override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


class ConfigError(ValueError):
    """配置缺失或非法 —— 启动期就暴露，不给「先跑起来再说」留余地。

    单独成类（而不是裸 ValueError）是为了让 `__main__` 能把它渲染成一条
    可读提示。刚克隆完仓库的人最可能缺的正是这几项，把 traceback 甩给他
    等于什么都没说。继承 ValueError，因为它确实描述的是「配置的取值不对」。
    """


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读取整型环境变量；未设置时返回 default，非法值直接报错。

    非法值不静默回退到 default —— 那会让「我明明设了却没生效」
    这类问题变得无法诊断。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        raise ConfigError(
            f"{name} 必须是整数，当前值是 {raw!r}。"
            f"请在 .env 中修正（例如 {name}={default}）。"
        ) from None
    if value < minimum:
        raise ConfigError(
            f"{name} 必须 >= {minimum}，当前值是 {value}。请在 .env 中修正。"
        )
    return value


def _env_dir(name: str, default: Path) -> Path:
    """读取目录型环境变量；未设置时返回 default，非法值直接报错。

    非法值不静默回退：把 WORKDIR 拼错却以为生效了，比直接报错难查得多。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    path = Path(raw.strip()).expanduser().resolve()
    if not path.is_dir():
        raise ConfigError(
            f"{name} 必须是一个已存在的目录，当前值是 {path}。"
            f"请先创建该目录，或在 .env 中修正 {name}。"
        )
    return path


def _require_env(name: str, hint: str) -> str:
    """读取必填环境变量；缺失时给出「缺什么 + 去哪补」。

    **首次部署最容易漏的一步就是把 .env.example 复制成 .env。** 这种情况下
    `os.environ[name]` 抛出的裸 KeyError 只说明「少了这一项」，不说明该在哪填、
    该填成什么样 —— 而这恰恰是新用户唯一需要知道的事。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raise ConfigError(
            f"{name} 未设置 —— {hint}\n"
            f"修复：在仓库根目录（即 README.md 所在目录）创建 .env 并填入该项：\n"
            f"    cp .env.example .env        # Windows: copy .env.example .env\n"
            f"若 .env 已存在，说明里面缺少或注释掉了这一行。"
        )
    return raw.strip()


# Agent 的工作目录。默认为仓库根；设置环境变量 WORKDIR 可指向别处，
# 用于让 Agent 在另一个项目里工作 —— 此时该目录同时成为权限边界。
WORKDIR = _env_dir("WORKDIR", REPO_ROOT)

# 会话数据目录
SESSION_DIR = REPO_ROOT / ".myharness" / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# 审计日志文件
AUDIT_LOG = REPO_ROOT / ".myharness" / "audit.jsonl"

# 长期记忆目录（跨会话共享）
MEMORY_DIR = REPO_ROOT / ".myharness" / "memory"

# 定时任务：状态文件（任务登记表）与每次运行的会话留档目录
SCHEDULE_STATE = REPO_ROOT / ".myharness" / "schedules.json"
SCHEDULE_DIR = REPO_ROOT / ".myharness" / "scheduled"

# 初始化安全策略和审计日志
init_policy(WORKDIR)
init_audit_logger(AUDIT_LOG)


def get_workdir() -> Path:
    """当前工作目录（运行时读取）。

    不要用 `from config import WORKDIR` 缓存结果 —— 那是 import 时的快照，
    一个可变的全局变量被冻结进了另一个模块。
    """
    return WORKDIR


@contextmanager
def use_workdir(path: Path | str):
    """临时把工作目录切到 path，作用域结束后无条件恢复。

    工具层（tools._get_workdir）与权限层（security 的 policy）必须**同时**切换，
    否则会出现「审计记录 A，文件实际写到 B」。把两者绑在同一处，使它们无法分叉
    —— 这是本函数存在的唯一理由。

    这是对 AGENTS.md「禁止可变全局状态」的**受控例外**，与 guards 同类：
    生命周期被限制在 with 块内，且退出时恢复。
    长期方案是把 workspace 作为显式参数传给 agent_loop / assemble_tool_pool，
    但那要先改 hook 签名（hook 目前只有 block 一个参数，表达不了 per-agent 策略）。
    """
    global WORKDIR
    target = Path(path).resolve()
    if not target.is_dir():
        raise ValueError(f"workspace must be an existing directory, got {target}")

    previous = WORKDIR
    WORKDIR = target
    init_policy(target)
    try:
        yield target
    finally:
        WORKDIR = previous
        init_policy(previous)

memory_store = MemoryStore(MEMORY_DIR)

# Provider 配置（必须在 install_default_hooks 之前，因为提取 hook 需要 provider）
PROVIDER_TYPE = (os.getenv("PROVIDER") or "anthropic").strip()
MODEL = _require_env(
    "MODEL_ID",
    "它是模型的名字，例如 claude-sonnet-4-6 / qwen-plus / glm-4-flash。",
)

# 单次模型回复的 token 上限（Anthropic API 的 max_tokens 参数）。
#
# 注意区分四个不同的上限，它们管的方向各不相同：
#   MAX_TOKENS         单次回复的**输出**长度（本项）
#   MAX_CONTEXT_TOKENS 单次请求的**输入**规模（超了压缩历史）
#   TURN_TOKEN_BUDGET  一轮对话的**累计**消耗（超了结束本轮）
#   MAX_TURNS          一轮对话的**请求次数**（超了结束本轮，兜底）
#
# 设得太小会在模型写大文件时截断工具参数，表现为
# "missing required positional arguments" 这类难懂的错误。
# 各 provider 上限不同，所以做成可配置项而不是写死在 loop 里。
DEFAULT_MAX_TOKENS = 8000
MAX_TOKENS = _env_int("MAX_TOKENS", DEFAULT_MAX_TOKENS)

# 单次请求的**输入**上限：历史估算超过它的 80% 就压缩。
#
# 这才是「上下文窗口」这一侧的护栏。它与 MAX_TOKENS 是两个方向：
# MAX_TOKENS 管模型这次能写多长，这里管我们这次发过去多长。
# 默认 100k 是给 128k 窗口的模型留出回答空间；用 32k 窗口的模型请调小，
# 否则请求会先被上游以「context length exceeded」拒绝（provider 会报出来，
# 但那是失败而不是压缩）。
MAX_CONTEXT_TOKENS = _env_int("MAX_CONTEXT_TOKENS", 100_000)

# 一轮用户消息的**累计**消耗上限（成本护栏）。
#
# 注意：每轮请求都会把整段历史重新发一遍，所以这个值按「每次请求的
# 输入 + 输出」累加，增长远快于上下文本身。它只用来兜住失控的循环
# （例如模型反复调工具），不是日常限制：正常的十几轮工具调用约几十万。
# 轮次本身的兜底见下面的 MAX_TURNS。
TURN_TOKEN_BUDGET = _env_int("TURN_TOKEN_BUDGET", 1_000_000)

# 单次 agent_loop 的模型请求轮数上限。
#
# 语义是「**一次用户提问**内的最大模型请求次数」，不是整个会话的总轮数 ——
# 用户再问一句就重新计数。所以它可以设得很大。
#
# 它只是第四层兜底。前三层刹车都在 guards.py，按「无进展」「连续失败」
# 和 token 预算精确终止；把轮次调大之所以安全，正是因为那三层先拦住了
# 原地打转。反过来，**只调大轮次而不加刹车就是把死循环的暴露时间往后推** ——
# 这也是这项改动必须和 ProgressGuard 一起上的原因。
MAX_TURNS = _env_int("MAX_TURNS", 200)

# 从注册表获取 Provider 类并实例化
ProviderClass = PROVIDER_REGISTRY.get(PROVIDER_TYPE)
if not ProviderClass:
    available = ", ".join(PROVIDER_REGISTRY.keys())
    raise ConfigError(
        f"未知的 PROVIDER: '{PROVIDER_TYPE}'。可选值：{available}。"
        f"请在 .env 中修正 PROVIDER。"
    )

# 凭据在启动时就检查，不推迟到第一次请求。
#
# openai SDK 会在构造 client 时直接抛 OpenAIError，那是一个 import 期的裸
# traceback；anthropic SDK 则放行到请求时才失败，于是错误出现在「问了一句话
# 之后」，更难定位。两者都换成一句「缺哪个变量、去哪填」。
_REQUIRED_CREDENTIAL = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}
_credential = _REQUIRED_CREDENTIAL.get(PROVIDER_TYPE)
if _credential:
    _require_env(
        _credential,
        f"PROVIDER={PROVIDER_TYPE} 用它作为 API 凭据（第三方兼容端点见 .env.example 注释）。",
    )

# 根据 Provider 类型选择对应的配置
if PROVIDER_TYPE == "openai":
    provider = ProviderClass(
        model=MODEL,
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL"),  # 支持千问等兼容端点
    )
elif PROVIDER_TYPE == "anthropic":
    provider = ProviderClass(
        model=MODEL,
        api_key=os.getenv("ANTHROPIC_API_KEY"),
        # 自定义 base_url（如智谱 GLM）走构造参数。
        # 以前这里是构造完再替换 provider.client，那条支路会丢掉
        # 构造时设置的超时与 max_retries —— 换端点就等于换了一套故障表现。
        base_url=os.getenv("ANTHROPIC_BASE_URL"),
    )
else:
    provider = ProviderClass(model=MODEL)

# 装配内置 hook（权限、审计、日志、记忆检索、Stop、自动提取记忆）。
# 必须在 provider 之后，因为 extract_memories_hook 需要调用模型。
install_default_hooks(memory_store, provider)

# 技能目录启动时扫描一次，之后只读；system prompt 只含目录，不含技能全文
skill_library = SkillLibrary([BUILTIN_SKILL_DIR])

# system prompt 必须**逐字节稳定**。
#
# 它在请求前缀里位于 messages 之前（[tools][system][messages...]），所以只要
# 有一位变化，后面的整段对话历史都会失去前缀缓存 —— 对话越长，代价越大。
# 因此这里不放任何随用户输入变化的内容，包括长期记忆召回：记忆改由
# UserPromptSubmit hook 注入到**当前用户消息**里（见 bootstrap.install_default_hooks），
# 那段本来每轮就是新的，不会作废已有前缀。
def build_system_prompt(workspace: Path | None = None) -> str:
    """构建 system prompt。**不接收用户输入**，保证跨请求逐字节相同。

    Args:
        workspace: 本次会话的工作目录；None 时回退到全局 WORKDIR

    为什么必须接受显式 workspace：``agent_loop`` 的 workspace 可以不是全局
    WORKDIR —— TeamAgent 传给每个 worker 的是 ``.team/<agent>/workspace``。
    只读全局值会让模型以为自己在仓库根，用相对路径去操作越界文件，然后被
    safe_path 一条条拒绝，表现为反复重试同一批失败调用。
    """
    target = Path(workspace) if workspace is not None else WORKDIR
    base = f"You are a coding agent at {target}. Use tools to solve tasks.Your current operating environment is Windows 11 + PowerShell"
    return "\n\n".join(filter(None, [base, skill_library.catalog()]))
