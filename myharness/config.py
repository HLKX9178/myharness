"""配置加载与共享运行时依赖。

这里是路径、Provider 的**唯一来源**。其他模块一律从这里导入，
不得各自计算路径或自行创建客户端。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from myharness.providers import PROVIDER_REGISTRY
from myharness.security import init_policy
from myharness.audit import init_audit_logger
from myharness.bootstrap import install_default_hooks
from myharness.memory import RECALL_LIMIT, MemoryStore
from myharness.skills import BUILTIN_SKILL_DIR, SkillLibrary

# 仓库根目录：<repo>/myharness/config.py -> <repo>/myharness -> <repo>
REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env", override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)


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
        raise ValueError(
            f"{name} must be an integer, got {raw!r}. "
            f"Fix {name} in .env (for example {name}={default})."
        ) from None
    if value < minimum:
        raise ValueError(
            f"{name} must be >= {minimum}, got {value}. Fix {name} in .env."
        )
    return value

# Agent 的工作目录。
# 目前指向仓库根目录，方便它读取项目文档。
# 后续会改为独立的 workspace/，避免 Agent 修改到自身源码。
WORKDIR = REPO_ROOT

# 会话数据目录
SESSION_DIR = REPO_ROOT / ".myharness" / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# 审计日志文件
AUDIT_LOG = REPO_ROOT / ".myharness" / "audit.jsonl"

# 长期记忆目录（跨会话共享）
MEMORY_DIR = REPO_ROOT / ".myharness" / "memory"

# 初始化安全策略和审计日志
init_policy(WORKDIR)
init_audit_logger(AUDIT_LOG)

memory_store = MemoryStore(MEMORY_DIR)

# Provider 配置（必须在 install_default_hooks 之前，因为提取 hook 需要 provider）
PROVIDER_TYPE = os.getenv("PROVIDER", "anthropic")  # 默认使用 Anthropic
MODEL = os.environ["MODEL_ID"]

# 单次模型回复的 token 上限（Anthropic API 的 max_tokens 参数）。
#
# 注意与 loop.py 的 MAX_TOTAL_TOKENS 区分：那是本轮对话的**累计**预算，
# 这里限制的是**单次请求**的输出长度。
#
# 设得太小会在模型写大文件时截断工具参数，表现为
# "missing required positional arguments" 这类难懂的错误。
# 各 provider 上限不同，所以做成可配置项而不是写死在 loop 里。
DEFAULT_MAX_TOKENS = 8000
MAX_TOKENS = _env_int("MAX_TOKENS", DEFAULT_MAX_TOKENS)

# 从注册表获取 Provider 类并实例化
ProviderClass = PROVIDER_REGISTRY.get(PROVIDER_TYPE)
if not ProviderClass:
    available = ", ".join(PROVIDER_REGISTRY.keys())
    raise ValueError(
        f"Unknown provider: '{PROVIDER_TYPE}'. "
        f"Available providers: {available}"
    )

provider = ProviderClass(
    model=MODEL,
    api_key=os.getenv("ANTHROPIC_API_KEY")  # 未来可以根据 PROVIDER_TYPE 选择不同的 key
)

# 如果设置了自定义 base_url（目前仅支持 Anthropic）
if PROVIDER_TYPE == "anthropic" and os.getenv("ANTHROPIC_BASE_URL"):
    from anthropic import Anthropic
    provider.client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

# 装配内置 hook（权限、审计、日志、记忆检索、Stop、自动提取记忆）。
# 必须在 provider 之后，因为 extract_memories_hook 需要调用模型。
install_default_hooks(memory_store, provider)

# 技能目录启动时扫描一次，之后只读；system prompt 只含目录，不含技能全文
skill_library = SkillLibrary([BUILTIN_SKILL_DIR])

# Phase 5：system prompt 改为动态函数，根据用户查询注入记忆
def build_system_prompt(user_query: str = "") -> str:
    """构建 system prompt，可选注入相关记忆。"""
    base = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."

    parts = [base, skill_library.catalog()]

    # 召回相关记忆
    if user_query:
        memories = memory_store.search(user_query, limit=RECALL_LIMIT)
        if memories:
            mem_lines = [
                "## Long-term Memory",
                "The following facts were saved in previous sessions. "
                "They are for reference only — if they conflict with the current request, "
                "follow the current request.",
            ]
            for mem in memories:
                mem_lines.append(f"- [{mem['type']}] {mem['description']}")
            parts.append("\n".join(mem_lines))

    return "\n\n".join(filter(None, parts))

# 向后兼容：默认 system prompt 不含记忆
SYSTEM = build_system_prompt()
