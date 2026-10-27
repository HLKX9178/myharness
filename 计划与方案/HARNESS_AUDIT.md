# Harness 能力审计

> 对照《深入理解 AI Agent：设计原理与工程实践》（李博杰，v1.2）的 Harness 五功能框架。
>
> 定位：**不重复** `CODE_REVIEW.md`（哪里写错了）与 `DESIGN_REVIEW.md`（哪里想错了）。
> 这一份只审**能力缺口** —— 书里要求一个 harness 必须具备的五个功能，本项目各覆盖到什么程度，
> 以及哪些手段其实没有向模型注入任何它本来不知道的信息。
>
> 审计基线：41 个模块 / 7,105 行 / 45 次提交 / 268 个用例全绿（2026-10-17 实测）。

---

## 一句话结论

**这个 Harness 的重心是"别做错"，不是"做成"。**

约束层（权限、边界、守卫）做到了同类项目的中上水平，工具层干净，
但**验证层几乎是空的** —— 这恰好是书里认为 harness 与"会调工具的脚本"分道扬镳的那一层。

一个 Harness 成熟不成熟，不看它有多少工具，看它能不能回答一个问题：
**"这次真的做成了吗？"** 当前项目答不上来 —— 它只能回答"模型说做成了"。

---

## 一、五功能覆盖

| 功能 | 书里的判据 | 本项目现状 | 判定 |
|---|---|---|---|
| **工具**（能行动） | 通用优先，元能力收益最高 | 表驱动、定义与实现相邻、单点组装、MCP 前缀命名；`bash` 本身就是元能力 | **B+** 扎实 |
| **上下文**（支撑决策） | 预算按 token、结果入库即裁剪、隔离优于压缩 | 技能两段式合格；压缩是**按条数丢弃**；工具输出零裁剪；无状态栏 | **C** |
| **约束**（预防错误） | 能代码强制就别写成建议，fail-closed | 命令 token 判定 + 工作区边界 + 唯一 dispatch 入口，设计正；但默认放行、`confirm` 无人承接 | **B-** |
| **验证**（发现偏差） | 必须是**外部**判分，不接受自我声称 | loop 内零验证；判据只在事后跑，且是行为代理 | **D** |
| **纠正**（闭合循环） | 分级恢复：静默重试 → 降级接续 → 暴露用户 | provider 重试（好）+ 连续三败直接终止；无回滚、无检查点、无降级 | **D+** |

如果把五个功能画成一条闭合回路，本项目的情况是：**左边两段（工具、约束）厚，右边两段（验证、纠正）薄，而回路靠右边才闭得上。**

---

## 二、验证层：书里最重要的一层，这里几乎是空的

这不是"还没做"，而是**做了一半放错了位置**。三条事实：

### 1. 验收判据存在，但从不进入 loop

`EvalTask.success_criteria`（`eval.py:32`）就是一份现成的验收判据。全仓库只有一处调用：

```
myharness/eval.py:110     success = task.success_criteria(messages)
```

位于 `run_task()` 里 —— 即**任务跑完之后**由外部 Python 判定。
`agent_loop()` 从头到尾不知道判据的存在。

后果：模型停下来的那一刻，harness 没有任何独立信息可以判断"它做完了没有"。
`Stop` 事件目前注册的两个 hook（`session_summary_hook`、`MemoryExtractionHook`）
返回值都是 `None` —— 也就是说，`Stop` 具备"强制续跑"的机制，**但没有任何策略在使用它**。

这正是书里 10.x 反复讲的首要失败模式：**过早终止**。模型说"完成了"，harness 就收工。

### 2. 判据本身是行为代理，不是结果验证

看 `multi_create_and_verify`（`eval_tasks.py:159`）：

```python
user_input="Create a file named output.txt with 'Test content', then read it back to verify",
success_criteria=lambda msgs: has_tool_call(msgs, "write_file")
                             and has_tool_call(msgs, "read_file")
                             and has_text_output(msgs),
```

判据是"**调没调这两个工具**"。一个把 `output.txt` 写成空文件的模型，同样通过。
`basic_write_file` 也是同理（"调了 write_file 且没报错"）。

9 个任务里只有 `reasoning_calculate` 真的看了结果（`"56088" in str(msgs)`），
而它连"这个数字出现在哪个 tool_result 里"都不区分。

按书里的标准：**只检查行为轨迹的判据，度量的是"模型像不像在干活"，不是"活干成了没有"。**
这类判据会持续给出乐观信号 —— 和 `DESIGN_REVIEW.md` 第六节担心的里程碑虚标是同一个病。

### 3. 结果是：正确率数字不可信

README 里 `90.0%` 这样的通过率，实际含义是"90% 的任务里模型调用了预期工具且没说错话"。
它无法回答"文件内容对不对""测试过没过""数字算没算对"。

**这不是评测框架的 bug，是验证抽象缺失的必然结果。** 补多少任务也修不好。

---

## 三、两处与书里结论相反的做法

### 1. 动态信息被写进了 system prompt —— 每轮 KV Cache 全失效

`loop.py:105` 每次进入对话时调用 `build_system_prompt(user_query)`，
而 `config.py:230` 会按**用户当前这句话**去检索记忆并拼进 system。

书里 2.2 的三条铁律第一条是：**静态前缀绝不改动，动态信息只追加到末尾。**
这里正好相反 —— system 的内容取决于本轮用户问题，于是每来一个新问题，缓存的整段前缀作废。

雪上加霜：全仓库 `grep -i cache` **零命中**（7,105 行里没有一处 `cache_control`）。
也就是说，即便在同一轮内的多次工具调用之间（此时 system 确实不变），也没有显式标记缓存断点。

代价是纯成本问题：多轮对话的 input token 从头到尾按全价计费。
技能目录、记忆、工具定义这些"每轮都一样"的部分，本来是可以只付一次的钱。

**改法**（两处，都很小）：
- 记忆从 system 移到**第一条 user 消息的末尾**（或单独的 user 消息），system 只留真正静态的部分；
- 在 Anthropic provider 里给 tools 末尾与 system 末尾加 `cache_control: {"type": "ephemeral"}`。

### 2. 上下文满了整段丢弃 —— 书里明确站"隔离优于压缩"

`context.py` 的策略是 `messages[-30:]`，超出的部分**永久消失**：
原始任务、用户约束、已确认的决策全部不留痕迹。

书里 2.x 的取向是反的：优先让海量中间信息**根本不进主上下文**，
进了的要**在入库那一刻**裁剪并标注省略量，而不是等满了再整段丢。

已验证的连带后果（`DESIGN_REVIEW.md` C1 已指出，此处补实测）：

```
31 条消息 → should_compress=True → 压缩后首条 role = "assistant" → 长度 30
```

首次压缩就会让历史以 `assistant` 开头，下一轮请求 400。
`should_compress` 在 31 条时就触发，所以这个问题**第 16 轮左右必现**，不是边界情况。

---

## 四、"新信息判据"逐层审计

书里那条判据可以压成一句话：**这项手段，有没有向上下文注入一条模型在生成时根本无法获得的信息？**
拿它过一遍现有的全部机制：

| 机制 | 注入的信息 | 模型原本能知道吗 | 判定 |
|---|---|---|---|
| 工具输出（`tool_result`） | 外部世界的真实状态 | 不能 | **真信息** |
| 工作区路径边界拒绝 | "这条路径越界了" | 不能 | **真信息**（且是硬约束） |
| `RepeatCallGuard` | "你刚用同样参数调过一次" | 不能（跨轮记忆不可靠） | **真信息**，但只拦一次，偏保守 |
| MCP 工具发现 | 服务端真实具备的能力 | 不能 | **真信息** |
| 技能两段式 | 目录（元数据）+ 按需全文 | 不能 | **真信息**，且是 token 效率的正确答案 |
| 记忆召回 | 上次会话的"事实" | 不能 | **弱信息** —— 见下 |
| `ConsecutiveFailureGuard` 的反馈 | "请检查参数格式或换一种方法" | 能 —— 工具报错原文就在上一条 `tool_result` 里 | **接近零信息** |
| `large_output_hook` 告警 | 只打印给人看，不进上下文 | — | **不进上下文**（不算 harness 手段） |
| 上下文压缩 | 无（只做删除） | — | **负信息**（丢失已知事实） |
| 无 token 预算决策 | 无 | — | 缺失 |

两条值得单独说：

**记忆召回是"自我强化的回路"，不是外部信息。**
`extract_memories_hook` 让模型自己判断"什么值得记住"，下个会话再原样注入 system。
中间没有任何外部校验。模型这次记错的结论，下次会被当成前提。
`CODE_REVIEW.md` 的 S6 讲的是它作为**注入面**的风险（对），但更日常的风险是：
**它把模型的猜测固化成了事实**，而书里 10.4.3 论证的正是"无外部反馈的自我纠正无效"。
这个机制是那条结论的一个变种 —— 自我记忆同样无效。

**失败反馈是纯 token 支出。**
`guards.py:71` 的那句"请检查参数格式、路径是否正确，或尝试换一种方法"，
在 `tool_result` 已经带着真实报错的情况下，没有增加任何新信息。
判据在这里很好用：**答不出"它注入了什么新信息"，这一层就是心理安慰。**

---

## 五、最高杠杆的一处改动：把验证接成 Stop hook

`Stop` 已经能强制续跑（`loop.py:146`），缺的只是策略。
加一个验证 hook，harness 立刻从"模型说完成就完成"变成"外部判据说完成才完成"。

### 最小版（约 40 行，零配置）

```python
# myharness/verification.py
"""Stop hook：用外部判据核对完成度；不通过则注入失败摘要并强制续跑。

判据必须读外部状态（文件内容 / 命令退出码 / 测试结果），
绝不问模型"你做完了吗" —— 那是书里 10.4.3 证明无效的自我纠正。
"""

import subprocess
from pathlib import Path

MAX_VERIFY_ROUNDS = 2          # 防止验证-修复无限循环
_rounds = 0


def _detect_test_entry(workspace: Path) -> list[str] | None:
    """零配置探测：有 tests/ 就跑 pytest，有 package.json 就跑 npm test。"""
    if (workspace / "tests").is_dir():
        return ["python", "-m", "pytest", "-q", "--maxfail=3"]
    if (workspace / "package.json").exists():
        return ["npm", "test", "--silent"]
    return None


def verification_hook(messages: list, workspace: Path | None = None) -> str | None:
    """返回 None = 放行；返回字符串 = 验证未通过，该字符串进入 messages 并续跑。"""
    global _rounds
    if _rounds >= MAX_VERIFY_ROUNDS or workspace is None:
        return None
    cmd = _detect_test_entry(workspace)
    if cmd is None:
        return None

    proc = subprocess.run(cmd, cwd=workspace, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    if proc.returncode == 0:
        return None

    _rounds += 1
    tail = (proc.stdout + proc.stderr)[-3000:]
    return (
        f"验证未通过（{' '.join(cmd)} 退出码 {proc.returncode}）。"
        f"这是真实的执行结果，不是猜测。请修复后重新验证：\n\n{tail}"
    )


def reset_verify_state() -> None:
    global _rounds
    _rounds = 0
```

接入三处：`bootstrap.py` 里 `register_hook("Stop", verification_hook)`；
`loop.py` 的 `reset_turn_state()` 里调 `reset_verify_state()`；
hook 签名已支持 `workspace`，`stop` 事件目前没传 —— 需要 `trigger_hooks("Stop", messages, workspace=workspace)`。

### 为什么这一版值得先做

- **它注入的是真信息**：pytest 的输出是模型无法凭空生成的；
- **成本极低**：一次 pytest 通常比一次模型调用便宜一到两个数量级；
- **不改变架构**：`Stop` 的续跑机制已经存在，只是没人用；
- **可度量**：改完之后 `eval` 的通过率会**掉一截** —— 那是诚实化，不是退步。

### 完整版的方向（之后再说）

把 `EvalTask.success_criteria` 泛化成任务级判据，在 `agent_loop` 入口一并传入：
用户在提需求时给出验收条件（"这个文件必须是合法 JSON，且有 `name` 字段"），
harness 把它编译成可执行检查，在 `Stop` 时跑。
这样"完成"就有了与模型无关的定义 —— 这才是书里说的 完成判定。

**不要做的事：加一个"审核 Agent"去问另一个模型"它做完了吗"。**
`CODE_REVIEW.md` 与书里 10.x 的结论一致：各方处理的是同一份文本，
不引入外部信号，多一层只会多一次成本。

---

## 六、现在不建议做的

按 README 自己的取向（工程严谨性 > 功能数量）排序，以下都属于"功能数量"：

- 分布式调度、向量记忆、更多 provider、更多内置工具；
- 重写 teams 为 DAG 编排 —— 先让**单** Agent 的完成判定可信，多 Agent 的验证只会更难；
- 上下文压缩升级成 LLM 摘要 —— 优先级低于工具输出入库裁剪（后者便宜且立刻见效）。

---

## 七、如果只做三件事

| 顺序 | 改动 | 理由 | 成本 |
|---|---|---|---|
| 1 | **验证 hook（Stop）** | 补上唯一缺失的功能，回路才闭合 | 半天 |
| 2 | **`cache_control` + 记忆移出 system** | 直接省真钱，且修正了一个与书相反的取向 | 半天 |
| 3 | **`context.compress` 首条必须是 user + 工具输出入库裁剪** | 必现 400，且丢失上下文是静默故障 | 半天 |

三件事都不改架构，都不增加工具数量，都直接提升"可信度"这一个指标。
