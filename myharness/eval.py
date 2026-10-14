"""评测框架 (M7 - 治理)。

提供：
1. 任务定义与管理
2. 自动化评测执行
3. 指标收集与报告生成
4. 基线对比

设计原则：
- 每个任务是独立的、可重现的
- 评测结果包含完整的上下文信息
- 支持增量评测和基线对比
"""

import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable, Literal

from myharness.loop import agent_loop
from myharness.config import provider


@dataclass
class EvalTask:
    """单个评测任务。"""

    task_id: str
    description: str
    user_input: str
    success_criteria: Callable[[list[dict]], bool]
    category: str = "general"
    max_turns: int = 10
    expected_cost_range: tuple[int, int] | None = None  # (min_tokens, max_tokens)


@dataclass
class EvalResult:
    """单次评测结果。"""

    task_id: str
    success: bool
    duration_seconds: float
    total_tokens: int
    num_turns: int
    final_messages: list[dict]
    error: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass
class EvalReport:
    """完整评测报告。"""

    timestamp: str
    total_tasks: int
    passed: int
    failed: int
    pass_rate: float
    avg_duration: float
    avg_tokens: int
    p50_duration: float
    p95_duration: float
    total_cost_usd: float
    results: list[EvalResult]
    baseline_delta: dict[str, float] | None = None


class EvalRunner:
    """评测执行器。"""

    def __init__(self, workspace: Path | None = None):
        """
        初始化评测器。

        Args:
            workspace: 评测工作目录
        """
        self.workspace = workspace or Path.cwd() / ".eval"
        self.workspace.mkdir(exist_ok=True)

    def run_task(self, task: EvalTask) -> EvalResult:
        """
        执行单个评测任务。

        Args:
            task: 评测任务

        Returns:
            评测结果
        """
        print(f"\n[EVAL] Running: {task.task_id} - {task.description}")

        # 准备消息
        messages = [{"role": "user", "content": task.user_input}]

        # 执行任务
        start_time = time.time()
        error = None

        try:
            # 在独立工作目录执行
            task_workspace = self.workspace / task.task_id
            task_workspace.mkdir(exist_ok=True)

            agent_loop(messages, workspace=task_workspace)

            # 评估成功条件
            success = task.success_criteria(messages)

        except Exception as exc:
            success = False
            error = f"{type(exc).__name__}: {exc}"

        duration = time.time() - start_time

        # 统计 token 使用
        total_tokens = self._count_tokens(messages)

        # 统计轮次
        num_turns = sum(1 for msg in messages if msg.get("role") == "assistant")

        result = EvalResult(
            task_id=task.task_id,
            success=success,
            duration_seconds=round(duration, 2),
            total_tokens=total_tokens,
            num_turns=num_turns,
            final_messages=messages,
            error=error,
        )

        status = "✓ PASS" if success else "✗ FAIL"
        print(f"[EVAL] {status} - {duration:.2f}s, {total_tokens} tokens, {num_turns} turns")

        return result

    def run_suite(self, tasks: list[EvalTask], baseline_path: Path | None = None) -> EvalReport:
        """
        运行整个评测套件。

        Args:
            tasks: 任务列表
            baseline_path: 基线报告路径（用于对比）

        Returns:
            评测报告
        """
        print(f"\n{'='*70}")
        print(f"Running Evaluation Suite: {len(tasks)} tasks")
        print(f"{'='*70}")

        results = []
        for task in tasks:
            result = self.run_task(task)
            results.append(result)

        # 生成报告
        report = self._generate_report(results)

        # 如果有基线，计算增量
        if baseline_path and baseline_path.exists():
            baseline = self._load_baseline(baseline_path)
            report.baseline_delta = self._calculate_delta(report, baseline)

        return report

    def save_report(self, report: EvalReport, output_path: Path) -> None:
        """
        保存评测报告。

        Args:
            report: 评测报告
            output_path: 输出路径
        """
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # 保存 JSON 格式
        report_dict = asdict(report)
        output_path.write_text(json.dumps(report_dict, indent=2, ensure_ascii=False), encoding="utf-8")

        print(f"\n[EVAL] Report saved to: {output_path}")

        # 打印摘要
        self._print_summary(report)

    def _count_tokens(self, messages: list[dict]) -> int:
        """估算 token 数量（简化版）。"""
        # 简化估算：4 字符 ≈ 1 token
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        total_chars += len(str(block))
        return total_chars // 4

    def _generate_report(self, results: list[EvalResult]) -> EvalReport:
        """生成评测报告。"""
        import statistics
        from datetime import datetime

        passed = sum(1 for r in results if r.success)
        failed = len(results) - passed

        durations = [r.duration_seconds for r in results]
        tokens = [r.total_tokens for r in results]

        # 计算成本（假设 $3/M input tokens, $15/M output tokens，简化为平均 $9/M）
        total_tokens = sum(tokens)
        total_cost = total_tokens * 9 / 1_000_000

        return EvalReport(
            timestamp=datetime.now().isoformat(),
            total_tasks=len(results),
            passed=passed,
            failed=failed,
            pass_rate=round(passed / len(results) * 100, 2) if results else 0,
            avg_duration=round(statistics.mean(durations), 2) if durations else 0,
            avg_tokens=int(statistics.mean(tokens)) if tokens else 0,
            p50_duration=round(statistics.median(durations), 2) if durations else 0,
            p95_duration=round(statistics.quantiles(durations, n=20)[18], 2) if len(durations) > 1 else 0,
            total_cost_usd=round(total_cost, 4),
            results=results,
        )

    def _load_baseline(self, path: Path) -> EvalReport:
        """加载基线报告。"""
        data = json.loads(path.read_text(encoding="utf-8"))
        # 简化：只加载需要对比的字段
        return EvalReport(**{k: v for k, v in data.items() if k != "results"}, results=[])

    def _calculate_delta(self, current: EvalReport, baseline: EvalReport) -> dict[str, float]:
        """计算相对基线的增量。"""
        return {
            "pass_rate_delta": round(current.pass_rate - baseline.pass_rate, 2),
            "avg_duration_delta": round(current.avg_duration - baseline.avg_duration, 2),
            "avg_tokens_delta": current.avg_tokens - baseline.avg_tokens,
            "cost_delta_usd": round(current.total_cost_usd - baseline.total_cost_usd, 4),
        }

    def _print_summary(self, report: EvalReport) -> None:
        """打印报告摘要。"""
        print(f"\n{'='*70}")
        print("Evaluation Summary")
        print(f"{'='*70}")
        print(f"Total Tasks:     {report.total_tasks}")
        print(f"Passed:          {report.passed} ({report.pass_rate}%)")
        print(f"Failed:          {report.failed}")
        print(f"Avg Duration:    {report.avg_duration}s")
        print(f"Avg Tokens:      {report.avg_tokens}")
        print(f"P50 Duration:    {report.p50_duration}s")
        print(f"P95 Duration:    {report.p95_duration}s")
        print(f"Total Cost:      ${report.total_cost_usd}")

        if report.baseline_delta:
            print(f"\nΔ vs Baseline:")
            for key, value in report.baseline_delta.items():
                sign = "+" if value > 0 else ""
                print(f"  {key}: {sign}{value}")

        print(f"{'='*70}\n")
