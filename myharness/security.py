"""安全策略：工作区边界检查与危险操作拦截。

本模块负责：
1. 路径校验：所有文件操作必须在 workspace 内
2. 命令检查：危险的 bash 命令被拒绝
3. 审计日志：记录所有策略决策（允许/拒绝）

命令检查按 **token** 判定，不做子串匹配。原因见下方 `_DANGEROUS_COMMANDS`
上方的注释 —— 子串匹配的误拒率太高，而误拒会让模型反复试同一条命令。

已知边界：只做**启发式**判定，不是能对抗刻意绕过的沙箱。`sh -c` / `eval`
里的命令会被递归剥开重查，但 `python -c "..."`、base64 解码后再执行这类
「换一个解释器」的写法挡不住 —— 要挡它们得做真正的语法分析。真正的边界
是 workspace 路径检查 + 审计，命令检查只是拦住手滑。
"""

import re
import shlex
from pathlib import Path
from typing import Any

from myharness.audit import get_audit_logger


# ============================================================
# 命令检查规则
# ============================================================
#
# 为什么不用子串匹配：`format`、`chmod 777`、`> /dev/` 这类模式会命中
#    printf "format"、black --format、echo hi > /dev/null
# 这些完全无害的命令。误拒的代价不只是这一次失败 —— 模型会以为是自己
# 写错，换个写法再试，而用户看到的是「一条正常命令被拒绝」却查不出原因。
#
# 所以这里先按 shell 的复合命令分隔符切段，再对每一段分词，只在
# 「命令名位置」和「参数形态」上匹配。子串匹配只保留给 fork bomb
# 这种不存在正当用法的字面形态。
#
# 覆盖面是**启发式**的：它挡的是「模型手滑造成的灾难性操作」，
# 不是一个能对抗刻意绕过的沙箱。真正的边界是 workspace 路径检查。

# 复合命令分隔符：一段命令的起点在这些符号之后
_SEGMENT_SPLITTERS = ("&&", "||", "$(", ";", "|", "&", "`", "\n")

# 包装命令：跳过后才是指令本体。
# 不跳过的话 `sudo rm -rf /` 的命令名是 "sudo"，危险命令会全部漏检。
_WRAPPERS = frozenset({
    "sudo", "doas", "command", "nohup", "nice", "time", "env", "xargs",
})

# 子 shell：`sh -c "<cmd>"` 里的那条命令要重新检查一遍。
# 只递归一层层剥开，不解析 shell 语法 —— 深度上限防止构造出的嵌套把栈打穿。
_SHELL_COMMANDS = frozenset({
    "sh", "bash", "zsh", "dash", "ksh", "ash", "cmd", "powershell", "pwsh",
})
_MAX_NESTING_DEPTH = 3

# 包装命令里**带一个值**的选项：`sudo -u root <cmd>` 中的 root 不是命令名。
# 只列出常见的提权/编排选项 —— 复刻完整的 getopt 表意义不大且会随版本过期，
# 目标是把「参数被误当成命令名」这个最常见的漏检堵上。
_WRAPPER_OPTIONS_WITH_VALUE = frozenset({
    "-u", "--user", "-g", "--group", "-p", "--prompt", "-h", "--host",
    "-C", "--close-from", "-D", "--chdir", "-R", "--chroot",
    "-T", "--command-timeout", "-t", "--type", "-r", "--role",
})

# 危险命令名（**整个 token 精确匹配**，不是子串）
_DANGEROUS_COMMANDS = frozenset({
    "format",   # Windows 磁盘格式化
    "fdisk",    # 分区表
    "parted",
    "wipefs",
    "shred",    # 覆写设备/文件
})

# 按前缀匹配的命令名：mkfs / mkfs.ext4 / mkfs.xfs ...
_DANGEROUS_COMMAND_PREFIXES = ("mkfs",)

# Windows 可执行后缀：`format.com` 与 `format` 是同一件事
_EXEC_SUFFIXES = (".exe", ".com", ".bat", ".cmd")

# 允许读写的伪设备：由内核提供，重定向过去无害。
# 白名单而不是黑名单 —— 设备名是无穷的，只有这几个是安全的。
_SAFE_DEVICE_NAMES = frozenset({
    "null", "zero", "full", "random", "urandom",
    "stdin", "stdout", "stderr", "tty",
})

# rm 的根/家目录目标：递归 + 强制删除命中这些才算危险。
# 判据是「目标是根」而不是「命令里出现 /」—— 否则 `rm -rf ./build` 会被误伤。
_ROOTISH_TARGETS = frozenset({
    "/", "/*", "/.", "/..", "//", "~", "~/", "~/*", "$HOME", "${HOME}",
})

# fork bomb：字面形态，没有正当用法，所以可以直接子串匹配
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:")

# 输出重定向的目标（`>` / `>>` 后面跟的路径）
_REDIRECT = re.compile(r"\d?>>?\s*([^\s;&|<>`]+)")

# 权限位是「所有人可写可执行」的字面量：777 / 0777 / 1777 ...
_MODE_ALL_WRITE = re.compile(r"[0-7]*777\Z")

# 符号权限里的全权限授予（a+rwx / ugo+rwx / +rwx）。
# 注意不含 `+x` —— 那是给脚本加执行位，日常且必需。
_SYMBOLIC_ALL_WRITE = re.compile(r"(?:[ugoa]*\+rwx|a=rwx)\Z")


def _strip_wrapping(token: str) -> str:
    """剥掉命令替换、引号、包裹括号留下的外壳。

    `$(rm -rf /)` 被分隔符切开后，目标会带着尾巴 `)`；不剥掉就认不出
    它是根目录，而 `printf "format"` 这类判定的前提也是先去掉引号。

    注意只剥 `$(` 这个**整体前缀**，不能写成 `lstrip("$(")` ——
    那会把 `$HOME` 里的 `$` 也吃掉，变出一个谁也认不出的 `HOME`。
    """
    value = token.strip().strip("'\"")
    if value.startswith("$("):
        value = value[2:]
    return value.rstrip(")")


def _command_name(token: str) -> str:
    """从一个 token 提取「命令名」：去外壳、取 basename、去可执行后缀、转小写。

    `C:\\Windows\\System32\\format.com` -> `format`
    `/usr/bin/mkfs.ext4`                -> `mkfs.ext4`

    整段被引号包住的命令串（`sh -c "rm -rf /"` 的载荷）在这里只剩一个 token，
    取它的第一个词 —— 否则 basename 会被末尾的 `/` 抹成空串，整段被当成
    「没有命令名」跳过。
    """
    name = _strip_wrapping(token).replace("\\", "/")
    if " " in name:
        name = name.split()[0]
    name = name.rsplit("/", 1)[-1].lower()
    for suffix in _EXEC_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _tokenize(segment: str) -> list[str]:
    """把一段命令切成 token。

    用 `posix=False`：posix 模式会把反斜杠当转义符，于是
    `C:\\Windows\\System32\\format.com` 被啃成 `C:WindowsSystem32format.com`，
    Windows 路径形态的危险命令直接漏检。非 posix 模式保留引号，由
    `_strip_wrapping` 统一剥掉 —— 一处处理比两处约定可靠。

    引号不闭合等情况会让 shlex 抛错 —— 此时退化为按空白切分。
    宁可切得粗一些（可能误拒），也不能因为解析失败就整段放行。
    """
    try:
        return shlex.split(segment, posix=False)
    except ValueError:
        return segment.split()


def _split_segments(command: str) -> list[str]:
    """按 shell 的复合命令分隔符把命令行切成若干段。"""
    parts = [command]
    for separator in _SEGMENT_SPLITTERS:
        expanded: list[str] = []
        for part in parts:
            expanded.extend(part.split(separator))
        parts = expanded
    return parts


def _is_assignment(token: str) -> bool:
    """`FOO=bar` 形态：env/nohup 自己的参数，不是命令名。"""
    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token) is not None


def _strip_wrappers(argv: list[str]) -> list[str]:
    """跳过 sudo/nohup/env 这类包装命令，露出真正的指令。

    包装命令自己还会带参数，其中「带值的选项」（`sudo -u root <cmd>` 的
    root）最容易把参数误当成命令名 —— 只跳过以 `-` 开头的 token 是不够的。
    """
    index = 0
    while index < len(argv):
        if _command_name(argv[index]) not in _WRAPPERS:
            break
        index += 1
        while index < len(argv):
            token = argv[index]
            if token in _WRAPPER_OPTIONS_WITH_VALUE:
                index += 2  # 选项 + 它的值
                continue
            # 其余参数：`-i` 这类选项、`FOO=bar` 赋值、`timeout 30` 的秒数
            if token.startswith("-") or _is_assignment(token) or token.isdigit():
                index += 1
                continue
            break
    return argv[index:]


def _nested_payload(argv: list[str]) -> str | None:
    """取出嵌套 shell 里的那条命令：`sh -c "<cmd>"` / `eval "<cmd>"`。

    这是旧实现唯一比 token 判定强的地方 —— 子串匹配顺带命中了嵌在字符串
    里的 `rm -rf /`。既然改成 token 判定会丢掉这部分覆盖，就显式补回来，
    而不是靠「顺带误伤」去覆盖。

    返回前要剥掉载荷外面的引号：分词保留引号，不剥的话递归进来又是一整块。
    """
    name = _command_name(argv[0])
    if name in _SHELL_COMMANDS:
        for index, token in enumerate(argv[1:], start=1):
            if token.lower() in ("-c", "/c", "-command", "/command"):
                rest = argv[index + 1:]
                return _strip_wrapping(" ".join(rest)) if rest else None
        return None
    if name == "eval":
        rest = argv[1:]
        return _strip_wrapping(" ".join(rest)) if rest else None
    return None


def _has_flag(flags: list[str], short: str, long: str) -> bool:
    """短选项（`-R`、`-rf` 这类簇）或长选项（`--recursive`）是否存在。"""
    for token in flags:
        if token.startswith("--"):
            if token[2:].split("=", 1)[0] == long:
                return True
        elif short in token[1:].lower():
            return True
    return False


def _is_block_device(path: str) -> bool:
    """路径是否指向 /dev 下的**真实**设备（而非 null/zero 这类伪设备）。"""
    normalized = _strip_wrapping(path).replace("\\", "/")
    if not normalized.startswith("/dev/"):
        return False
    return normalized.rsplit("/", 1)[-1] not in _SAFE_DEVICE_NAMES


def _check_rm(argv: list[str]) -> str | None:
    """rm：只有「递归 + 强制 + 目标是根/家目录」三项同时成立才拦。"""
    flags = [t for t in argv[1:] if t.startswith("-") and t != "-"]
    if not (_has_flag(flags, "r", "recursive") and _has_flag(flags, "f", "force")):
        return None
    for target in argv[1:]:
        if target.startswith("-"):
            continue
        if _strip_wrapping(target) in _ROOTISH_TARGETS:
            return f"rm -rf 作用于根目录或家目录 ({target})"
    return None


def _check_dd(argv: list[str]) -> str | None:
    """dd：读写目标是真实设备时拦截（dd if=/dev/zero of=/dev/sda）。"""
    for argument in argv[1:]:
        for key in ("if=", "of="):
            if argument.startswith(key) and _is_block_device(argument[len(key):]):
                return f"dd 直接读写块设备 ({argument})"
    return None


def _check_chmod(argv: list[str]) -> str | None:
    """chmod：授予「所有人可写可执行」时拦截。

    判的是权限位本身，不是字面子串 —— 于是 `chmod -R 777 x` 与
    `chmod 0777 x` 都拦得住，而 `chmod +x script.sh` 正常放行。
    """
    for argument in argv[1:]:
        if argument.startswith("-"):
            continue
        mode = _strip_wrapping(argument)
        if _MODE_ALL_WRITE.match(mode) or _SYMBOLIC_ALL_WRITE.match(mode):
            return f"chmod 授予全部权限 ({argument})"
    return None


def _check_redirect(command: str) -> str | None:
    """输出重定向到真实设备时拦截；`> /dev/null` 必须放行。"""
    for match in _REDIRECT.finditer(command):
        if _is_block_device(match.group(1)):
            return f"重定向写入块设备 ({match.group(1)})"
    return None


class SecurityPolicy:
    """安全策略：工作区边界 + 命令检查。"""

    def __init__(self, workspace: Path):
        """
        初始化安全策略。

        Args:
            workspace: 允许访问的工作区根目录（绝对路径）
        """
        self.workspace = workspace.resolve()

    def check_path(self, path: str | Path) -> tuple[bool, str]:
        """
        检查路径是否在工作区内。

        Args:
            path: 要检查的路径（可以是相对或绝对路径）

        Returns:
            (是否允许, 原因说明)
        """
        try:
            target = Path(path).resolve()

            # 检查是否在 workspace 内
            if self.workspace in target.parents or target == self.workspace:
                return True, "路径在工作区内"

            return False, f"路径 {target} 在工作区 {self.workspace} 之外"

        except Exception as e:
            return False, f"路径解析失败: {e}"

    def check_command(self, command: str) -> tuple[bool, str]:
        """
        检查 bash 命令是否安全。

        判定顺序：字面形态（fork bomb）→ 全串特征（重定向）→ 逐段命令名与参数。
        先全串再看单段，是因为重定向的目标可能落在命令名的段之外
        （例如 `cat x | tee y > /dev/sda`）。

        Args:
            command: 要执行的命令字符串

        Returns:
            (是否允许, 原因说明)
        """
        return self._check_command(command, depth=0)

    def _check_command(self, command: str, depth: int) -> tuple[bool, str]:
        """check_command 的递归主体：depth 是已剥开的子 shell 层数。"""
        text = command.strip()
        if not text:
            return True, "命令通过安全检查"

        if _FORK_BOMB.search(text):
            return False, "命令包含危险操作: fork bomb"

        reason = _check_redirect(text)
        if reason:
            return False, f"命令包含危险操作: {reason}"

        for segment in _split_segments(text):
            argv = _strip_wrappers(_tokenize(segment))
            if not argv:
                continue

            name = _command_name(argv[0])
            if not name:
                continue

            if name in _DANGEROUS_COMMANDS or name.startswith(
                _DANGEROUS_COMMAND_PREFIXES
            ):
                return False, f"命令包含危险操作: {name}"

            if name == "rm":
                reason = _check_rm(argv)
            elif name == "dd":
                reason = _check_dd(argv)
            elif name == "chmod":
                reason = _check_chmod(argv)
            else:
                reason = None

            if reason:
                return False, f"命令包含危险操作: {reason}"

            # 子 shell 里的命令要当成一条新命令重新检查
            payload = _nested_payload(argv)
            if payload and depth < _MAX_NESTING_DEPTH:
                allowed, nested_reason = self._check_command(payload, depth + 1)
                if not allowed:
                    return False, nested_reason

        return True, "命令通过安全检查"


# 全局策略实例（在 config.py 中初始化）
_policy: SecurityPolicy | None = None


def init_policy(workspace: Path) -> None:
    """初始化全局安全策略。"""
    global _policy
    _policy = SecurityPolicy(workspace)


def get_policy() -> SecurityPolicy:
    """获取全局安全策略实例。"""
    if _policy is None:
        raise RuntimeError("Security policy not initialized. Call init_policy() first.")
    return _policy


# ============================================================
# Hook：权限判定与审计
# ============================================================

# 需要路径边界检查的工具
_PATH_TOOLS = {"read_file", "write_file", "edit_file"}

# 需要命令黑名单检查的工具
_COMMAND_TOOLS = {"bash"}


def permission_hook(block: Any, workspace: Path | None = None) -> str | None:
    """PreToolUse：所有工具的权限判定，并记录审计日志。

    返回 None 表示放行；返回字符串表示拒绝，该字符串会成为 tool_result。

    这个 hook 位于 dispatch 路径上，所以**新增工具无法绕过它** ——
    只要工具走 execute_tool()，权限就一定被检查。

    注意：路径检查是相对 policy.workspace 解析的，不依赖进程当前目录。

    Args:
        block: tool_use block
        workspace: 工作目录；None 时使用全局 policy
    """
    # 使用显式传入的 workspace，否则回退到全局 policy
    if workspace is not None:
        policy = SecurityPolicy(workspace)
    else:
        policy = get_policy()

    name = block.name
    args = block.input or {}

    if name in _COMMAND_TOOLS:
        target = str(args.get("command", ""))
        allowed, reason = policy.check_command(target)
    elif name in _PATH_TOOLS:
        raw = str(args.get("path", ""))
        # 相对路径必须相对 workspace 解析，否则会跟着进程 CWD 漂移
        target = str(policy.workspace / raw)
        allowed, reason = policy.check_path(policy.workspace / raw)
    else:
        # 没有对应策略的工具默认放行：glob_files、技能/记忆工具，
        # 以及所有 mcp__* 外部工具。
        #
        # MCP 工具目前**没有**任何宿主侧限制。早期 MCPRegistry 为每个 MCP
        # 工具算过 allow / confirm / deny，但那份结果没有任何调用方读取，
        # dispatch 也从不检查 —— 属于「看起来有权限控制」的死代码，已删除。
        # 要收紧外部工具，就在这个分支里按 block.name 判定，别绕回注册表。
        return None

    get_audit_logger().log_decision(
        action=name,
        target=target,
        decision="allowed" if allowed else "denied",
        reason=reason,
    )

    if allowed:
        return None
    # 拒绝也走 "Error: " 前缀（契约 1：出错返回 "Error: ..."）。
    # 这不只是文案统一 —— ConsecutiveFailureGuard 靠这个前缀判定失败，
    # 用别的措辞会让「模型反复越界」完全不触发告警与中止。
    return f"Error: Permission denied: {reason}"
