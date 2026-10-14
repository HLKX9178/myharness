# myharness

**通用 Agent Harness 基础设施。**

模型提供智能，Harness 提供环境。本项目负责构建后者 —— 把模型接入真实环境所需的
全部能力做扎实：工具治理、权限与沙箱、状态与记忆、外部连接、并发调度、多 Agent
协作，以及可观测性与评测。

## 设计取向

本项目追求**工程严谨性而非功能数量**。一个边界清晰、行为可预测、结果可度量的
Harness，胜过一个功能很多但无法信任的 Harness。

三个不可动摇的取向：

- **模型无关** —— 通过 Provider 层适配，不绑定任何单一模型厂商。
- **核心可读** —— Agent Loop 保持短小、可直接阅读和调试，不依赖重量级框架。
- **行为可度量** —— 任何能力改进都必须能用评测数据证明。

## 环境要求

- Python 3.12+
- 一个 Anthropic 兼容的模型 API（Anthropic / GLM / Kimi / DeepSeek 等）

## 快速开始

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/yourusername/myharness.git
cd myharness

# 2. 创建虚拟环境
python -m venv .venv

# 3. 激活虚拟环境
# Windows
.\.venv\Scripts\Activate.ps1
# Linux/macOS
source .venv/bin/activate

# 4. 安装依赖
pip install -r requirements.txt

# 5. 配置 API Key
cp .env.example .env
# 编辑 .env 文件，填入你的 API Key
```

### 基本使用

```bash
# 启动交互式 CLI
python -m myharness

# 在对话中使用 Agent
> 创建一个 Python 文件计算斐波那契数列
```

### 运行评测

```bash
# 运行完整评测套件
python run_eval.py

# 运行特定分类
python run_eval.py --category basic

# 与基线对比
python run_eval.py --baseline .eval/reports/baseline.json --output .eval/reports/current.json
```

评测报告示例：
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

### 多 Agent 协作

```python
from myharness.tools import spawn_team, create_task, list_tasks

# 在对话中使用
> 启动 3 个 Agent 帮我处理这些任务
# Agent 会调用：spawn_team(3)
# 然后创建多个任务：create_task(...)
# 查看进度：list_tasks()
```

```sh
python -m venv .venv
.venv/Scripts/activate          # Windows
pip install -r requirements.txt
```

## 配置

```sh
cp .env.example .env
```

编辑 `.env`，至少填入 `ANTHROPIC_API_KEY` 与 `MODEL_ID`。

使用第三方 Anthropic 兼容 provider 时，额外设置 `ANTHROPIC_BASE_URL`，
具体取值见 `.env.example` 中的注释。

> `.env` 已被 `.gitignore` 排除。**切勿提交任何密钥。**

## 运行

```sh
python -m myharness
```

Windows 下也可直接双击 `run.bat`，或在 VS Code 中按 `F5` 启动（带断点调试）。

## 目录结构

```text
myharness/                  仓库根：只放工具链、文档与项目元数据
├── README.md               本文件
├── AGENTS.md               开发指南（Agent 与人类协作者共同遵守）
├── requirements.txt        依赖
├── run.bat                 Windows 一键启动
├── .vscode/                VS Code 调试与编辑器配置
│
└── myharness/              源码包
    ├── __main__.py         python -m myharness 入口
    ├── cli.py              终端交互
    ├── loop.py             Agent Loop
    ├── config.py           配置与共享依赖
    ├── tools.py            工具定义与实现
    └── ui.py               终端输出样式
```

**根目录只允许出现四类内容**：工具链文件、文档、入口脚本、顶层目录。
任何 Python 模块都不得直接放在仓库根目录。

规划中的模块划分、依赖方向与职责边界见 [`AGENTS.md`](AGENTS.md#3-架构与模块边界)。

## 参与开发

**开始任何改动前，请先阅读 [`AGENTS.md`](AGENTS.md)。**

其中定义了：

- 五条不可违反的契约（工具返回值、消息格式、定义与实现配对、错误处理、密钥管理）
- 模块边界与单向依赖规则
- 并行协作规则（多个 Agent 同时工作时的文件所有权与接口约定）
- 编码规范与提交规范
- 里程碑与验收标准
- 明确的非目标
- 提交前检查清单

这份文件同时面向人类协作者和 AI Agent，旨在让任何人在任何时刻进入项目，
都能按同一套标准工作。
