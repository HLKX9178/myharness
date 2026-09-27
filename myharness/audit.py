"""审计日志：记录所有操作的批准/拒绝历史。

用于回答"某次操作是谁在何时批准/拒绝的"（M2 验收标准）。
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Literal


class AuditLogger:
    """审计日志记录器。"""

    def __init__(self, log_file: Path):
        """
        初始化审计日志。

        Args:
            log_file: 日志文件路径（JSON Lines 格式）
        """
        self.log_file = log_file
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def log_decision(
        self,
        action: str,
        target: str,
        decision: Literal["allowed", "denied"],
        reason: str,
        user: str = "system",
    ) -> None:
        """
        记录一次安全策略决策。

        Args:
            action: 操作类型（read_file, bash, write_file 等）
            target: 操作目标（文件路径、命令等）
            decision: 决策结果（allowed/denied）
            reason: 决策原因
            user: 决策者（默认 system，未来支持人工审批）
        """
        record = {
            "timestamp": datetime.now().isoformat(),
            "action": action,
            "target": target,
            "decision": decision,
            "reason": reason,
            "user": user,
        }

        # 追加到日志文件（JSON Lines 格式）
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def query_history(self, action: str | None = None, limit: int = 100) -> list[dict]:
        """
        查询操作历史。

        Args:
            action: 筛选特定操作类型（None 表示查询所有）
            limit: 最多返回多少条记录

        Returns:
            记录列表（从新到旧）
        """
        if not self.log_file.exists():
            return []

        records = []
        with self.log_file.open("r", encoding="utf-8") as f:
            for line in f:
                record = json.loads(line.strip())
                if action is None or record["action"] == action:
                    records.append(record)

        # 返回最新的 limit 条
        return records[-limit:][::-1]


# 全局审计日志实例（在 config.py 中初始化）
_logger: AuditLogger | None = None


def init_audit_logger(log_file: Path) -> None:
    """初始化全局审计日志。"""
    global _logger
    _logger = AuditLogger(log_file)


def get_audit_logger() -> AuditLogger:
    """获取全局审计日志实例。"""
    if _logger is None:
        raise RuntimeError("Audit logger not initialized. Call init_audit_logger() first.")
    return _logger
