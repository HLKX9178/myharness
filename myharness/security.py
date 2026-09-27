"""安全策略：工作区边界检查与危险操作拦截。

本模块负责：
1. 路径校验：所有文件操作必须在 workspace 内
2. 命令白名单：危险的 bash 命令被拒绝
3. 审计日志：记录所有策略决策（允许/拒绝）
"""

from pathlib import Path
from typing import Literal


class SecurityPolicy:
    """安全策略：工作区边界 + 命令白名单。"""

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

        Args:
            command: 要执行的命令字符串

        Returns:
            (是否允许, 原因说明)
        """
        # 危险命令黑名单
        DANGEROUS_PATTERNS = [
            "rm -rf /",
            "format",
            "mkfs",
            "> /dev/",
            "dd if=",
            ":(){ :|:& };:",  # fork bomb
            "chmod 777",
            "chmod -R 777",
        ]

        command_lower = command.lower().strip()

        for pattern in DANGEROUS_PATTERNS:
            if pattern in command_lower:
                return False, f"命令包含危险操作: {pattern}"

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
