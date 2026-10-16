# myharness

**通用 Agent Harness 基础设施。**

模型提供智能，Harness 提供环境。本项目负责构建后者 —— 把模型接入真实环境所需的
全部能力做扎实：工具治理、权限与沙箱、状态与记忆、外部连接、并发调度、多 Agent
协作，以及可观测性与评测。

> **Agency 来自模型训练，不来自外围代码。**
> 本项目不创造智能，只负责构建模型运行所需的环境。

---

## 设计取向

本项目追求**工程严谨性而非功能数量**。一个边界清晰、行为可预测、结果可度量的
Harness，胜过一个功能很多但无法信任的 Harness。

三个不可动摇的取向：

- **模型无关** —— 通过 Provider 层适配，不绑定任何单一模型厂商。
- **核心可读** —— Agent Loop 保持短小、可直接阅读和调试，不依赖重量级框架。
- **行为可度量** —— 任何能力改进都必须能用评测数据证明。

对应的工程约束写在 [`AGENTS.md`](AGENTS.md) 的「不可违反的契约」一节，
并由 `tests/test_hooks.py` 中的架构约束测试强制检查。

---

## 能力概览

| 里程碑 | 能力 | 状态 |
|---|---|---|
| M0 | 地基：Provider 抽象，Agent Loop 不依赖具体模型 SDK | ✅ |
| M1 | 单 Agent 可用：工具注册系统与基础工具集 | ✅ |
| M2 | 状态与安全：会话持久化、上下文压缩、权限与审计 | ✅ |
| M3 | 扩展机制：技能加载、跨会话长期记忆 | ✅ |
| M4 | 外部连接：MCP（stdio + Streamable HTTP） | ✅ |
| M5 | 并发与调度：后台任务、定时器、状态恢复 | ✅ |
| M6 | 多 Agent 协作：共享任务板、进程隔离、崩溃接管 | ✅ |
| M7 | 治理：可复现的评测报告 | ✅ |
| M8 | 交付：文档、架构图与 ADR | ✅ |

---

## 环境要求

- **Python 3.12+**
- **一个模型 API**，二选一：
  - Anthropic 兼容端点（Anthropic / 智谱 GLM / Kimi / DeepSeek）
  - OpenAI 兼容端点（阿里云百问 / OpenAI 等）
- 无重量级核心依赖：运行时只需要 `anthropic`、`openai`、`python-dotenv`、`croniter`

---

## 快速开始

### 1. 安装

```bash
git clone https://github.com/HLKX9178/myharness.git
cd myharness

python -m venv .venv

# Windows
.venv\Scripts\activate
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置

```bash
cp .env.example .env
```

编辑 `.env`，**至少**填入 API Key 与模型 ID：

```bash
PROVIDER=anthropic                  # anthropic | openai
MODEL_ID=claude-sonnet-4-6
ANTHROPIC_API_KEY=sk-your-key-here
```

> `.env` 与 `.env.*` 均已被 `.gitignore` 排除。**切勿提交任何密钥。**

### 3. 运行

```bash
python -m myharness
```

Windows 下也可双击 `run.bat`，或在 VS Code 中按 `F5` 启动（带断点调试）。

```
YOU >> 创建一个 Python 文件计算斐波那契数列
BOT >> ...
```

---

## 配置项

全部通过 `.env` 提供。除 `MODEL_ID` 外均有默认值。

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `PROVIDER` | 否 | `anthropic` | 选择 provider 实现：`anthropic` 或 `openai` |
| `MODEL_ID` | **是** | — | 模型 ID。缺失时启动即报错，不做静默回退 |
| `ANTHROPIC_API_KEY` | 视 provider | — | 使用 `anthropic` provider 时必填 |
| `ANTHROPIC_BASE_URL` | 否 | 官方端点 | 走第三方 Anthropic 兼容端点时设置 |
| `OPENAI_API_KEY` | 视 provider | — | 使用 `openai` provider 时必填 |
| `OPENAI_BASE_URL` | 否 | 官方端点 | 走千问等 OpenAI 兼容端点时设置 |
| `MAX_TOKENS` | 否 | `8000` | 单次模型回复的 token 上限 |
| `REQUEST_TIMEOUT_SECONDS` | 否 | `120` | 单次模型请求超时（连接超时固定 10s） |
| `WORKDIR` | 否 | 仓库根目录 | Agent 的工作目录，**同时是权限边界** |

`.env.example` 中每个可选项都写了「为什么需要它」以及典型误配的症状，
建议连同注释一起读。几个容易踩的点：

- **`MAX_TOKENS` 设太小**，模型写大文件时工具参数会被截断，报错形如
  `write_file() missing 2 required positional arguments` —— 看起来像模型不会用工具，
  实际是输出被截断。
- **不设 `REQUEST_TIMEOUT_SECONDS` 的代价**：两个 SDK 默认超时都是 600 秒，
  叠加 providers 层的 3 次尝试，一次卡死的请求能让进程静默占住近半小时。
  多 Agent 场景下表现为该 worker 停止轮询、它认领的任务被永久占住。
- **`WORKDIR` 会盖过命令行环境变量**（`load_dotenv(override=True)`）。
  想临时切目录，请不要把它写进 `.env`。

### 切换到千问（Qwen）

```bash
PROVIDER=openai
MODEL_ID=qwen-plus
OPENAI_API_KEY=sk-your-dashscope-api-key
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

完整步骤与模型清单见 [`QWEN_SETUP.md`](QWEN_SETUP.md)。

---

## 内置工具

工具由模型自主调用。定义与实现写在同一位置（契约 3），
新增一个工具只需要改一个文件。

| 工具 | 用途 |
|---|---|
| `bash` | 在工作目录内执行 shell 命令（经命令安全检查） |
| `read_file` | 读取文件（可指定行范围） |
| `write_file` | 写入/覆盖文件 |
| `edit_file` | 精确字符串替换编辑 |
| `glob_files` | 按模式匹配文件路径 |
| `load_skill` | 加载技能全文（技能目录已注入 system prompt） |
| `connect_mcp` | 连接 MCP server 并注册其工具 |
| `spawn_team` | 启动 N 个协作 Agent 进程 |
| `stop_team` | 停止协作 Agent |
| `create_task` | 向共享任务板投递任务 |
| `list_tasks` | 查看任务状态与认领者 |
| `get_task_result` | 查询某个任务的执行结果 |
| `send_team_message` | 向指定 Agent 发消息 |

此外，MCP server 发现的工具会在连接后动态并入工具池（命名前缀 `mcp__`）。

---

## 架构

```
用户输入
   │
   ▼
agent_loop()            loop.py —— 只保留循环不变量
   │
   ▼
execute_tool()          dispatch.py —— 唯一的工具执行入口
   │
   ├─ PreToolUse   ── permission_hook → repeat_call_guard → log_hook
   ├─ handler      ── 工具实现（永远返回字符串）
   └─ PostToolUse  ── result_hook → failure_guard → large_output_hook
```

**横切逻辑一律实现为 hook，不写进 Agent Loop。** 四个扩展点：

| 事件 | 时机 | 典型用途 |
|---|---|---|
| `UserPromptSubmit` | 用户输入进入 `messages` 之前 | 上下文注入、输入拦截 |
| `PreToolUse` | 工具执行之前 | 权限校验、重复调用拦截 |
| `PostToolUse` | 工具执行之后 | 审计、指标、大输出摘要 |
| `Stop` | 一轮结束 | 记忆提取、会话小结 |

hook 返回 `None` 表示放行；返回其它值表示拦截，该值短路后续 hook 并成为
`tool_result`。`UserPromptSubmit` 例外：多个 hook 的字符串结果**合并而不短路**，
返回 `Block(reason)` 才拒绝输入。

**为什么这是硬约束**：权限判定若依赖「每个工具自己记得调用」，新增工具就会
**静默绕过**安全检查。hook 位于 dispatch 路径上，使绕过在结构上不可能。

模块依赖方向、职责边界与完整架构图见
[`ARCHITECTURE.md`](ARCHITECTURE.md) 与 [`AGENTS.md`](AGENTS.md#3-架构与模块边界)；
关键取舍的「为什么这样选」见 [`ADR.md`](ADR.md)。

---

## 模型调用与错误诊断

Provider 层负责三件事：**接口统一、错误归一、重试归属**。

- SDK 异常在这里被翻译成 `ProviderError`，**不允许泄漏给 `agent_loop`**。
- 模型调用失败时，CLI 打印诊断并优雅结束本轮（不写半截 assistant 消息，
  下一轮可以直接重问），而不是抛 traceback 把会话一起丢掉。
- 只重试「重试可能成功」的错误（超时 / 连接失败 / 429 / 5xx，最多 3 次、
  尊重 `retry-after` 且封顶 8s）。**401 / 403 / 404 属于永久拒绝，一次即止** ——
  重试只会让你白等。
- SDK 自带重试被显式关闭（`max_retries=0`），避免与本层重试叠加放大。

诊断输出会带上状态码、服务端业务错误码、已尝试次数与可执行的修复建议。
例如账号未开通模型权限时（单次尝试，不重试）：

```
  [error] 模型调用失败: HTTP 403 AccessDenied.Unpurchased: Access to model denied.
  [error] 账号无权调用该模型（403）。常见原因是模型未开通/未购买或额度用尽：
          请到控制台开通该模型，或把 .env 的 MODEL_ID 换成有权限的模型。
          403 属于永久性拒绝，重试无用。
  本轮已结束，历史保留，修正后可重新提问。
```

网络抖动时则会先看到退避重试，最终输出形如 `模型调用失败（已尝试 3 次）: 请求超时`。

---

## 多 Agent 协作

多个 Agent 以 **独立进程** 并行工作，通过文件系统共享任务板与消息队列，
各自持有隔离的工作目录。

```python
from myharness.tools import spawn_team, create_task, list_tasks
```

```
> 启动 3 个 Agent 帮我处理这些任务
   模型调用 spawn_team(3) → 再调用 create_task(...) 投递任务
   查看进度：list_tasks()
```

- **崩溃接管**：任务带超时，认领者崩溃后会被其他成员重新认领。
- **消息投递**：不丢失、不重复。
- **文件隔离**：每个 Agent 有独立 workspace，并行修改互不干扰。

---

## MCP 集成

支持 **stdio** 与 **Streamable HTTP** 两种传输；工具以 `mcp__<server>__<tool>`
前缀注册，命名冲突会被检测。

授权策略完全由宿主侧配置决定，**不采信 server 自述**。可运行的接入示例见
[`examples/`](examples/)（`mcp_demo.py`、`mcp_stdio_example.py`、
`mcp_agent_integration.py`）。

---

## 测试

```bash
pytest tests/                 # 全量
pytest tests/test_hooks.py    # 单个文件
```

覆盖点包括：Hook 语义与架构约束、工具契约、权限与命令安全、上下文压缩、
会话持久化、MCP、调度与持久化、多 Agent 编排与崩溃恢复、Provider 错误归一。

> 多 Agent 相关用例会启动真实子进程，子进程冷启动需要数秒，
> 因此这些用例采用轮询等待而非固定 sleep。全量约 30～60 秒。

---

## 评测

```bash
python run_eval.py                          # 全部任务
python run_eval.py --category basic         # 指定分类
python run_eval.py --baseline .eval/reports/baseline.json \
                   --output .eval/reports/current.json
```

报告包含任务集规模、通过率、平均成本、P50/P95 延迟，以及相对基线的增量：

```
======================================================================
Evaluation Summary
======================================================================
Total Tasks:     10
Passed:          9 (90.0%)
Failed:          1
Avg Duration:    3.45s
Avg Tokens:      1234
P50 Duration:    2.8s
P95 Duration:    6.2s
Total Cost:      $0.0111

Δ vs Baseline:
  pass_rate_delta: +5.0
  avg_tokens_delta: -150
======================================================================
```

---

## 目录结构

```text
myharness/                    仓库根：只放工具链、文档与项目元数据
├── README.md                 本文件
├── AGENTS.md                 开发指南（Agent 与人类协作者共同遵守）
├── ARCHITECTURE.md           架构图与数据流
├── ADR.md                    架构决策记录
├── QWEN_SETUP.md             切换到千问的步骤
├── requirements.txt          依赖
├── run.bat                   Windows 一键启动
├── run_eval.py               评测入口
├── conftest.py               测试夹具
├── .vscode/                  VS Code 调试与编辑器配置
│
├── myharness/                源码包
│   ├── __main__.py           python -m myharness 入口
│   ├── cli.py                终端交互
│   ├── loop.py               Agent Loop 编排（不含横切逻辑）
│   ├── hooks.py              Hook 注册表
│   ├── dispatch.py           唯一的工具执行入口
│   ├── guards.py             重复调用 / 连续失败守卫
│   ├── bootstrap.py          内置 hook 的集中装配点
│   ├── config.py             配置加载与共享依赖
│   ├── providers/            模型 Provider 抽象与适配
│   ├── tools.py              工具定义与实现 + assemble_tool_pool
│   ├── security.py           权限策略 + permission_hook
│   ├── audit.py              审计日志
│   ├── observability.py      日志 / 告警 / 小结 hook
│   ├── context.py            上下文压缩
│   ├── session.py            会话持久化
│   ├── memory.py             长期记忆与提取 hook
│   ├── skills/               技能目录（*.md 为内置技能）
│   ├── mcp/                  MCP 客户端与注册表
│   ├── teams/                多 Agent 协作
│   ├── runtime/              后台任务与定时器
│   └── ui.py                 终端输出样式
│
├── tests/                    测试（必须入库）
└── examples/                 可运行示例
```

**根目录只允许出现四类内容**：工具链文件、文档、入口脚本、顶层目录。
任何 Python 模块都不得直接放在仓库根目录。

---

## 能力边界

诚实标注本项目**不能**做什么：

- **不训练或微调模型** —— Agency 来自模型，本项目只构建环境。
- **不是某一个模型的专用封装** —— 通过 Provider 层保持模型无关。
- **不追求功能数量** —— 优先保证已有能力的可靠性与可度量性。
- **不是低代码平台** —— 本项目是代码库，不做可视化拖拽编排。
- **不声称「生产级」** —— 这是一个个人项目，多 Agent 协作采用文件系统通信，
  适合单机、中等规模的任务，不适合分布式部署。
- **安全策略是「防误操作」而非「防恶意攻击」** —— 命令检查与路径边界能拦住
  模型的手滑，但不是一个针对攻破者的沙箱。

---

## 参与开发

**开始任何改动前，请先阅读 [`AGENTS.md`](AGENTS.md)。**

其中定义了：

- 六条不可违反的契约（工具返回值、消息格式、定义与实现配对、错误处理、密钥管理、横切逻辑 hook 化）
- 模块边界与单向依赖规则
- 并行协作规则（多个 Agent 同时工作时的文件所有权与接口约定）
- 编码规范与提交规范（Conventional Commits）
- 里程碑与验收标准
- 明确的非目标
- 提交前检查清单

这份文件同时面向人类协作者和 AI Agent，旨在让任何人在任何时刻进入项目，
都能按同一套标准工作。
