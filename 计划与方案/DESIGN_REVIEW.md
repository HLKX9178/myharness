# 设计评审：各部分思路是否合格

> 视角：把 myharness 当作"练手 harness 原理"的项目来评，**不评功能多少，只评设计取向对不对**。
> 上一份 `CODE_REVIEW.md` 讲的是"哪里写错了"；这一份讲"哪里想错了"。

## 一句话结论

**骨架合格，肌肉不均。** 最核心的三件套（薄 loop / hook 解耦 / 唯一 dispatch 入口）的设计思路是整份代码里最值钱的部分，比很多"生产项目"做得干净；但**上下文预算、权限回路、可观测性**这三块——恰恰是 harness 区别于"会调工具的脚本"的地方——抽象选错了或只做了一半。

| 部分 | 设计思路 | 判定 |
|---|---|---|
| Agent Loop | 薄循环，只留不变量 | **合格**（A-） |
| Hook 系统 | 四事件 + 短路语义 + 注册序 | **合格**，但返回值通道混用（B+） |
| dispatch / 契约 1 | 字符串边界，executor 极简 | **合格**，但抽象不够用（B-） |
| tools 表驱动 | 定义与实现相邻 | **合格**（B） |
| 权限 security | token 判定 + 诚实标注边界 | **思路对，落地残缺**（C+） |
| 上下文 context | 保留最近 N 条 | **不合格**（D） |
| 会话 session | JSON 快照 | **偏弱**（C+） |
| 记忆 memory | 文件 + 提取 + 整理 | **合格**（B-） |
| 技能 skills | 两段式加载 | **合格**（A-） |
| MCP | 前缀命名 + 宿主策略 | **设计对，接线断**（C） |
| runtime | 线程池 + cron + 状态文件 | **库合格，未集成**（D+） |
| teams | 进程隔离 + 文件通信 | **"协作"名不副实**（C） |
| eval / 可观测 | 内置评测 + 基线对比 | **方向对，度量不硬**（C+） |

---

## 一、想对的，别动

**1. 薄 loop + hook 解耦（这是全项目的灵魂）**

`loop.py` 118 行，只剩"问模型 → 执行工具 → 回填"和六个循环不变量。策略全部外挂。这个切分的判断标准（"可替换的策略在 hook，循环自身的不变量在 loop"）写得比绝大多数同类项目清楚，而且配了架构约束测试（`test_hooks.py` 用源码断言禁止 loop 访问 `TOOL_HANDLERS`）——**用测试锁住设计意图，这是很成熟的做法**，大部分所谓生产项目都做不到。

**2. 契约 3：定义与实现相邻**

牺牲了"分层整洁"，换来了"不可能出现定义了没实现"。这个取舍是对的，因为它消灭的是一整类静默故障。

**3. providers 的错误归一**

把 SDK 异常翻译成带 `retryable/status/hint/attempts` 的结构，并显式关掉 SDK 自己的重试（避免 3×3=9 次叠加）。这是"错误是数据"原则的正面示范，不是套话。

**4. 技能两段式**

目录进 prompt、正文按需拉取。这是 token 效率的标准答案，实现也干净。

**5. 诚实的能力边界**

README 明说"不是沙箱""不声称生产级"，security 的 docstring 明说"挡不住 `python -c`"。**知道自己没做什么并写下来，本身就是设计能力**。这一点比功能数量值钱。

---

## 二、抽象选错了（要重做，不是修补）

### D1 · 上下文管理用"消息条数"度量"token 预算"——本项目最根本的错位

`ContextManager(max_messages=30)`。问题是：**harness 的稀缺资源是 token，不是消息条数。** 一条 `cat` 出来的 100KB 工具结果和一条 10 字的用户消息，在"30 条"这个尺度上是等价的，在 token 尺度上差 4 个数量级。

这一个错误抽象连带三个后果：

1. **压缩时机错**：可能 8 条消息就 120k token（大文件读入），也可能 40 条才 3k token（短问答）。按条数判永远踩不到点上。
2. **压缩策略错**：直接丢弃最老的消息。丢弃意味着**原始任务、用户约束、已确认的决策全部消失**——模型在第 16 轮之后还在干活，但已经不记得为什么干。这比"报错"更危险，因为它不报错。
3. **工具输出不裁剪**：tool_result 原样入上下文。真正的 harness 会在**结果入库那一刻**就做 head/tail 截断并标注"已省略 N 行"，而不是等上下文满了再整段丢。

**改进方向**（按收益排序）：

- 用 provider 返回的 `usage.input_tokens` 做真实预算（`loop.py` 已经在累加 `total_tokens`，只是没用来做决策）；
- 压缩改成"保留首条 user + 最近 N 轮 + 中间段 LLM 摘要"，摘要里必须包含**当前任务目标与已完成事项**；
- tool_result 入库前裁剪（单条上限 ~10k 字符，超出保留头尾各 2k + 省略标记）；
- 预算分层：system / 技能目录 / 记忆 / 摘要 / 最近对话 各自配额，而不是一个总数。

这一条做完，项目的"可靠性"观感会有质变——比再加十个工具强得多。

### D2 · 权限只有"判定"，没有"回路"

`host_policy` 有三态 `allow / confirm / deny`，`permission_hook` 只会返回 `None` 或错误串。**`confirm` 没有承接者**：没有 UI 问"是否允许执行这条命令？"，所以 `confirm` 必然退化成 `allow`；`deny` 则因为策略没接线而彻底失效。

harness 的权限本质是**人机协作协议**（谁在什么时候批准了什么），不是一张黑名单。现在这套只有"自动拒绝危险命令"，缺了最核心的一半。

**改进方向**：

- `confirm` → `permission_hook` 返回一个待决状态，CLI 弹 `[y]允许 / [a]本次会话都允许 / [n]拒绝`，决定写回审计日志；
- 策略档位显式化（`read-only` / `workspace-write` / `danger-full-access`），按档位决定哪些工具默认放行、哪些必须确认——这比现在"三个工具走检查、其余默认放行"的 if 分支可维护得多；
- 默认分支改成"未知工具 → 需确认"，而不是"未知工具 → 放行"。安全默认必须是 fail-closed。

### D3 · 字符串契约承载不了"结果的语义"

契约 1 要求工具永远返回 str，这个边界的价值我认（executor 极简、模型一定能读到可自愈的文本）。但**把 str 当成 harness 内部的唯一表示**就过头了：

- `guards` 靠 `output.startswith("Error:")` 判失败 → 任何正常内容以 "Error:" 开头就误判，任何"部分成功"无法表达；
- 无法传"是否被截断""耗时多少""是否命中缓存"，所以 PostToolUse 里的审计/指标只能拿字符串做前缀判断；
- 无法表达多模态 tool_result（图片/附件需要 block 结构）。

**改进方向**：内部返回 `ToolResult(text, is_error, duration_ms, truncated, meta)`，**只在 dispatch 边界序列化成 str 交给模型**。契约"模型只看到字符串"完整保留，harness 内部恢复结构化。这是投入产出比最高的一次抽象修正。

### D4 · 横切原则只用在工具上，没用在模型调用上

项目反复强调"横切逻辑不写进 loop"，但**模型调用这条最重要的路径上没有 hook**：

- token 统计、成本、耗时的 `print` 直接写在 `loop.py:137`（这就是被原则禁止的硬编码，只是没人管它）；
- 没有 `PreModelCall` / `PostModelCall`，所以无法统一做请求日志、缓存、限流、prompt 版本记录；
- 结果：所谓"可观测性"模块只剩三个 print hook，M7"治理 ✅"名不副实。

**判断标准一致的话，模型调用才更该 hook 化**——它才是花钱、最慢、最需要审计的那一步。

---

## 三、只做了一半的（概念对，落地断）

| 部分 | 断在哪 | 补法 |
|---|---|---|
| **MCP** | 策略算了不用（`_policies` 丢弃）；transport 无超时、stderr 不 drain；握手后未发 `notifications/initialized` | 把 policy 接进 dispatch；transport 加读超时 + stderr 丢弃；握手补完 |
| **runtime** | `background/scheduler` 纯库，CLI 无入口、模型无工具 → M5 ✅ 但端到端不可用 | 加 `schedule_task` / `list_jobs` 工具，或把 ✅ 降级为"库就绪" |
| **teams** | 只有抢占队列；`send_team_message` 发出去没人消费（worker 里是 TODO）；无 DAG、无聚合 | 要么加 orchestrator + 结果聚合，要么改名"并行执行器" |
| **teams 并发** | `TaskBoard` 用 `threading.Lock`（进程内）实现"原子 claim"；`FileLock` 写了但没接 | claim 走 `FileLock`；JSONL 定期 compaction |
| **memory** | 检索是词袋重叠（`len(A∩B)/min`），无 IDF、无时间衰减、无 embedding | 练手阶段够用，但要在文档里写明"这是关键词召回，不是语义召回" |
| **eval** | token 用 `len//4` 估算、成本用固定单价、P95 拿 10 个样本算、不进 CI | 直接取 provider 的 usage；任务数 ≥30 再谈 P95；加 pass@k 与回归门禁 |
| **session** | 每轮全量重写、无临时文件+rename、无 schema 版本号 | 原子写 + `version` 字段 + 迁移钩子 |

---

## 四、结构层面的两个隐患

### S-1 · `config.py` 变成了组装根（God Module）

它 import 了 providers / security / audit / bootstrap / memory / skills，并在 **import 期**做完了：读 `.env`、建目录、初始化 policy 与 logger、构造 provider、安装 hook、扫描技能、拼 system prompt。

后果：

1. **任何模块 import config 都会触发全量初始化**——测试必须先造 `MODEL_ID`、`API_KEY`，否则连 `from myharness.context import ContextManager` 都跑不起来（我这次验证不得不绕开 config）；
2. **单向依赖名存实亡**：`memory`/`skills` 反过来被 config 依赖，而 config 被所有人依赖 → 事实上全是环；
3. `build_system_prompt` 属于"prompt 组装"，却住在配置层，还依赖三个全局单例。

**改进**：拆成三件——`config.py`（只读 env，纯数据）/ `prompt.py`（输入 workdir+skills+memories，输出字符串，纯函数）/ `app.py`（唯一的组装根：建 provider、装 hook、实例化 store）。测试时可以只 import 前两个。

### S-2 · 全局单例太多，且语义不同

`_policy`、`_logger`、`memory_store`、`skill_library`、`provider`、`_registry`、`_active_team`、`_manager`、`_scheduler`、`repeat_call_guard`、`failure_guard`。

它们被 AGENTS.md 分为"受控例外"和"违反"，但**对使用者来说长得一模一样**。多 agent 同进程场景下（`use_workdir` 那条注释自己承认了）会互相污染。

**改进**：引入一个 `Runtime` 上下文对象（provider / policy / stores / hooks 都挂上去），由入口创建并显式传参。哪怕只做一半（工具与 hook 层接受 `Runtime`），也比 11 个模块级全局变量清晰。

---

## 五、如果只能改 5 件事

按"对 harness 原理的学习价值 × 改动成本"排序：

1. **token 预算 + 摘要式压缩 + 工具输出入库裁剪**（D1）——这是 harness 里最难的工程，也是最有含金量的一块。工作量中等，收益最大。
2. **把 `confirm` 接通到 CLI，并引入三档策略**（D2）——权限从"黑名单"升级为"协议"，同时顺手让 MCP deny 生效。
3. **内部 `ToolResult`，边界仍返回 str**（D3）——让审计/指标/截断有地方挂。
4. **加 `PostModelCall` hook + JSONL 遥测**（D4）——把已有的 token 数字变成可被 eval 消费的事件，M7 才站得住。
5. **`config` 拆成 config / prompt / app**（S-1）——解锁可测试性，之后所有改动都会变快。

**不建议现在做的**：分布式调度、向量记忆、更多 provider、更多内置工具。这些是"功能数量"，不是"harness 能力"——而 README 自己说了，后者才是目标。

---

## 六、一个非技术但很关键的判断

README 的里程碑表 M4/M5 标了 ✅，但 M4 的授权策略不生效、M5 的能力没有入口。**对一个以"可信任、可度量"为卖点的项目，文档承诺与实际行为的偏差，比代码 bug 更伤**——因为读者是按文档建立信任的。

建议：把里程碑的验收标准改成"**端到端可用**"（有 CLI 入口或模型可调用的工具 + 一条通过的集成测试），否则这张表会持续给出乐观信号，而你会在别处为它付账。
