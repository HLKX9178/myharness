#!/usr/bin/env python3
"""运行评测套件。

用法：
    python run_eval.py                    # 运行所有任务
    python run_eval.py --category basic   # 运行指定分类
    python run_eval.py --baseline report.json  # 与基线对比
"""

import sys
from pathlib import Path

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from myharness.eval import EvalRunner
from myharness.eval_tasks import ALL_TASKS, get_tasks_by_category


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run evaluation suite")
    parser.add_argument(
        "--category",
        type=str,
        choices=["basic", "reasoning", "error_handling", "multi_step"],
        help="Run only tasks in this category",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        help="Path to baseline report for comparison",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(".eval/reports/latest.json"),
        help="Output path for report (default: .eval/reports/latest.json)",
    )

    args = parser.parse_args()

    # 选择任务
    if args.category:
        tasks = get_tasks_by_category(args.category)
        print(f"Running {len(tasks)} tasks in category: {args.category}")
    else:
        tasks = ALL_TASKS
        print(f"Running all {len(tasks)} tasks")

    if not tasks:
        print("No tasks to run")
        return 1

    # 运行评测
    runner = EvalRunner()
    report = runner.run_suite(tasks, baseline_path=args.baseline)

    # 保存报告
    runner.save_report(report, args.output)

    # 返回状态码
    return 0 if report.pass_rate == 100 else 1


if __name__ == "__main__":
    sys.exit(main())
