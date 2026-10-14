# 工作区隔离重构总结

## 改动目标

将工作区（workspace）从**可变全局状态**改为**显式参数传递**，消除技术债，符合 AGENTS.md 的设计原则。

## 核心改动

### 1. **hooks.py** - Hook 机制支持 workspace 参数

**改动**：
- 新增 `_invoke_hook()` 辅助函数，通过 `inspect.signature` 自动判断 hook 是否接受 workspace 参数
- `trigger_hooks()` 和 `trigger_all_hooks()` 增加可选的 `workspace` 参数
- 向后兼容：旧 hook 不需要修改，新 hook 可选择性接收 workspace

**影响**：
- 支持 per-agent 策略（不同 Agent 可以有不同的工作区）
- 保持向后兼容（旧 hook 签名仍然有效）

```python
# 旧 hook（仍然兼容）
def my_hook(block: Any) -> str | None:
    ...

# 新 hook（可选接收 workspace）
def my_hook(block: Any, workspace: Path | None = None) -> str | None:
    ...
```

---

### 2. **dispatch.py** - 工具执行入口传递 workspace

**改动**：
- `execute_tool()` 新增可选 `workspace` 参数
- 调用 handler 时通过 `inspect.signature` 检查是否支持 workspace 参数
- 调用 hook 时传递 workspace

**影响**：
- 工具函数可以接收显式的 workspace，不再依赖全局状态
- 向后兼容：不接受 workspace 的旧工具仍然可用

---

### 3. **tools.py** - 工具函数接收 workspace 参数

**改动**：
- `_get_workdir(workspace: Path | None = None)` - 优先使用显式 workspace，否则回退到全局
- 所有内置工具函数增加可选 `workspace` 参数：
  - `run_bash(command, workspace=None)`
  - `read_file(path, workspace=None)`
  - `write_file(path, content, workspace=None)`
  - `edit_file(path, old_text, new_text, workspace=None)`
  - `run_glob(pattern, workspace=None)`
  - `connect_mcp(name, workspace=None)`
- `safe_path(p, workspace=None)` 接收 workspace 参数
- `assemble_tool_pool(workspace: Path | None = None)` 接收 workspace 参数

**影响**：
- 工具函数不再依赖全局 `WORKDIR`
- 支持多 Agent 并发，每个 Agent 在独立工作区执行

---

### 4. **security.py** - 权限 hook 接收 workspace

**改动**：
- `permission_hook(block, workspace=None)` 接收 workspace 参数
- 优先使用显式 workspace 创建临时 SecurityPolicy，否则使用全局 policy

**影响**：
- 权限边界可以 per-agent 设置
- 审计日志记录的路径与实际工作区一致

---

### 5. **loop.py** - Agent Loop 传递 workspace

**改动**：
- `agent_loop(messages, workspace=None)` 接收可选 workspace 参数
- 未传入时使用 `config.get_workdir()` 获取全局 WORKDIR
- 调用 `assemble_tool_pool(workspace)` 时传递 workspace
- 调用 `execute_tool(block, handlers, workspace)` 时传递 workspace

**影响**：
- 上层调用方可以显式指定工作区
- CLI 使用全局 WORKDIR（默认行为不变）
- TeamAgent 传入独立 workspace（实现真正的隔离）

---

### 6. **teams/agent.py** - TeamAgent 使用显式传参

**改动前**：
```python
with use_workdir(self.workspace):
    agent_loop(self.messages)
```

**改动后**：
```python
agent_loop(self.messages, workspace=self.workspace)
```

**影响**：
- 不再依赖全局状态切换（`use_workdir` 上下文管理器）
- 代码更清晰，调用链上显式传递 workspace
- 删除了对 `config.use_workdir` 的导入

---

## 删除的技术债

### config.py 中的 `use_workdir()` 上下文管理器

**状态**：保留但标记为 deprecated（用于向后兼容，未来可移除）

这个函数曾经是「禁止可变全局状态」的**受控例外**，注释中明确说明：
```python
# 这是对 AGENTS.md「禁止可变全局状态」的**受控例外**
# 长期方案是把 workspace 作为显式参数传给 agent_loop / assemble_tool_pool
```

**现在这个"长期方案"已经完成**，`use_workdir()` 不再被内部代码使用。

---

## 测试验证

### 测试结果
```
173 passed in 11.87s
```

### 关键测试通过
- ✅ `test_workspace_isolation` - TeamAgent 工作区隔离
- ✅ `test_file_isolation_behavior` - 文件操作不泄漏到仓库根
- ✅ `test_use_workdir_switches_both_layers` - 工具边界与权限边界同步（向后兼容）
- ✅ 所有权限测试 - permission_hook 正确工作
- ✅ 所有 hook 测试 - 新的 workspace 参数机制正确工作

---

## 向后兼容性

所有改动都保持**完全向后兼容**：

1. **Hook 签名**：旧 hook 不需要修改，新 hook 可选择性接收 workspace
2. **工具函数**：workspace 参数都是可选的，默认值 None 表示使用全局 WORKDIR
3. **agent_loop**：未传入 workspace 时使用全局配置
4. **CLI 行为**：不变，仍然使用 `config.WORKDIR`

---

## 改动文件统计

```
myharness/dispatch.py      | 24 +++++++++++++++++++----
myharness/hooks.py         | 31 ++++++++++++++++++++++++++----
myharness/loop.py          | 21 +++++++++++++++-----
myharness/observability.py |  2 +-
myharness/security.py      | 13 +++++++++++--
myharness/teams/agent.py   | 15 +++++----------
myharness/tools.py         | 48 ++++++++++++++++++++++++++++------------------
7 files changed, 109 insertions(+), 45 deletions(-)
```

**核心改动**：+109 行，-45 行

---

## 收益

### 1. **架构清晰**
- 依赖关系显式化：调用链上清楚地看到 workspace 如何传递
- 消除隐式全局状态：不再需要在运行时修改全局变量

### 2. **多 Agent 安全**
- 真正的工作区隔离：每个 TeamAgent 的文件操作限制在自己的 workspace
- 权限边界一致：工具层、权限层、审计层都基于同一个 workspace

### 3. **可测试性**
- 测试可以传入临时 workspace，不影响全局状态
- 并发测试更安全

### 4. **符合设计原则**
- 满足 AGENTS.md 第 7 节「工程原则」中的「显式优于隐式」
- 消除了「禁止可变全局状态」的例外

---

## 后续可选优化

1. 移除 `config.use_workdir()`（已不再被内部使用）
2. 将 `config.WORKDIR` 改为私有变量（外部通过 `get_workdir()` 访问）
3. Hook 签名可以在未来强制要求 workspace 参数（破坏性变更，需要版本升级）

---

## 提交建议

```
refactor(workspace): 工作区改为显式参数传递，消除全局状态

将 workspace 从可变全局状态改为在调用链上显式传递：

- hooks: 支持可选的 workspace 参数，向后兼容旧签名
- dispatch: execute_tool 接收并传递 workspace
- tools: 所有工具函数接收可选 workspace 参数
- security: permission_hook 支持 per-agent 策略
- loop: agent_loop 接收 workspace 并传递给工具池和执行器
- teams: TeamAgent 通过显式参数传递 workspace，删除 use_workdir

向后兼容：workspace 参数全部可选，默认值 None 表示使用全局 WORKDIR。

测试：173 passed

符合 AGENTS.md 设计原则：显式优于隐式，消除可变全局状态。

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
```
