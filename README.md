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

## 安装

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
python agent.py
```

Windows 下也可直接双击 `run.bat`，或在 VS Code 中按 `F5` 启动（带断点调试）。

## 目录结构

结构随能力逐步建立，当前已有：

```text
agent.py            Agent 主循环与工具集
AGENTS.md           开发指南（Agent 与人类协作者共同遵守）
.env.example        配置模板
requirements.txt    依赖
run.bat             Windows 一键启动
.vscode/            VS Code 调试与编辑器配置
```

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
