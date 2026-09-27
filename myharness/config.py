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

# 仓库根目录：<repo>/myharness/config.py -> <repo>/myharness -> <repo>
REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env", override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# Agent 的工作目录。
# 目前指向仓库根目录，方便它读取项目文档。
# 后续会改为独立的 workspace/，避免 Agent 修改到自身源码。
WORKDIR = REPO_ROOT

# 会话数据目录
SESSION_DIR = REPO_ROOT / ".myharness" / "sessions"
SESSION_DIR.mkdir(parents=True, exist_ok=True)

# 审计日志文件
AUDIT_LOG = REPO_ROOT / ".myharness" / "audit.jsonl"

# 初始化安全策略和审计日志
init_policy(WORKDIR)
init_audit_logger(AUDIT_LOG)

# Provider 配置
PROVIDER_TYPE = os.getenv("PROVIDER", "anthropic")  # 默认使用 Anthropic
MODEL = os.environ["MODEL_ID"]

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

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."
