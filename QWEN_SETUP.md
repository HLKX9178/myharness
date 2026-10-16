# 切换到千问（Qwen）指南

## 快速切换

### 1. 更新 `.env` 配置

将 `.env` 文件改成以下内容（参考 `.env.qwen`）：

```bash
# Provider 类型
PROVIDER=openai

# 模型名称（千问支持的模型）
MODEL_ID=qwen-plus

# API Key（从阿里云百炼平台获取）
OPENAI_API_KEY=sk-your-dashscope-api-key-here

# API 端点（千问 OpenAI 兼容端点）
OPENAI_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
```

### 2. 获取 API Key

1. 访问 [阿里云百炼平台](https://bailian.console.aliyun.com/)
2. 进入"API-KEY 管理"
3. 创建新的 API-KEY
4. 复制 API-KEY 到 `.env` 文件的 `OPENAI_API_KEY`

### 3. 测试配置

直接启动 harness 发一句话即可验证——配置有问题时它会打印出诊断
（状态码、服务端错误码、修复建议），而不是抛 traceback：

```bash
run.bat
```

进入对话后随便输入一句话，能正常得到回复就说明配置正确。

> 早期这里引用的是仓库根目录下的探针脚本（`test_qwen.py` 等）。
> 按 AGENTS.md 的「根目录规则」，这些脚本已移出仓库根目录，
> 因此不再作为验证手段；`run.bat` 走的是同一条 provider 调用路径。

### 4. 正常使用

```bash
run.bat
```

---

## 支持的模型

| 模型名称 | 说明 | 适用场景 |
|---|---|---|
| `qwen-plus` | 通用模型（推荐） | 日常对话、编程 |
| `qwen-turbo` | 快速模型 | 快速响应场景 |
| `qwen-max` | 最强模型 | 复杂任务 |
| `qwen-coder-plus` | 代码模型 | 专注编程任务 |
| `qwen3.6-plus` | 最新版本 | 最新能力 |

---

## 切换回智谱 GLM

将 `.env` 改回：

```bash
PROVIDER=anthropic
MODEL_ID=glm-4-flash
ANTHROPIC_API_KEY=<你的智谱 API Key>
ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic
```

---

## 技术细节

- 千问使用 **OpenAI 兼容格式**，通过 `OpenAIProvider` 调用
- 智谱 GLM 使用 **Anthropic 兼容格式**，通过 `AnthropicProvider` 调用
- Provider 层抽象让切换模型零代码改动

---

## 参考文档

- [千问 API 文档](https://help.aliyun.com/zh/model-studio/developer-reference/use-qwen-by-calling-api)
- [OpenAI 兼容 API 参考](https://help.aliyun.com/document_detail/3016808.html)
