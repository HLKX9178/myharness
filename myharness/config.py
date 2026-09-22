"""配置加载与共享运行时依赖。

这里是路径、模型客户端的**唯一来源**。其他模块一律从这里导入，
不得各自计算路径或自行创建客户端。
"""

import os
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

# 仓库根目录：<repo>/myharness/config.py -> <repo>/myharness -> <repo>
REPO_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(REPO_ROOT / ".env", override=True)
if os.getenv("ANTHROPIC_BASE_URL"):
    os.environ.pop("ANTHROPIC_AUTH_TOKEN", None)

# Agent 的工作目录。
# 目前指向仓库根目录，方便它读取项目文档。
# 后续会改为独立的 workspace/，避免 Agent 修改到自身源码。
WORKDIR = REPO_ROOT

MODEL = os.environ["MODEL_ID"]

client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks."
