#!/usr/bin/env python3
"""测试流式 bash 输出。

验证长时运行的命令会实时显示进度。
"""

import sys
from pathlib import Path

# 添加父目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from myharness.tools import run_bash


def main():
    print("=" * 70)
    print("测试流式 Bash 输出")
    print("=" * 70)

    # 测试 1: 快速命令（无明显流式效果）
    print("\n[Test 1] 快速命令")
    print("-" * 70)
    result = run_bash("echo 'Hello World'")
    print(f"\n返回结果: {result}")

    # 测试 2: 带延迟的命令（应该看到流式输出）
    print("\n[Test 2] 模拟长时任务（循环输出）")
    print("-" * 70)

    # Windows 和 Unix 兼容的延迟命令
    import platform
    if platform.system() == "Windows":
        # Windows: 使用 PowerShell 或批处理循环
        cmd = """powershell -Command "for ($i=1; $i -le 10; $i++) { Write-Host \"Processing step $i/10...\"; Start-Sleep -Milliseconds 500 }" """
    else:
        # Unix: 使用 bash 循环
        cmd = """for i in {1..10}; do echo "Processing step $i/10..."; sleep 0.5; done"""

    result = run_bash(cmd)
    print(f"\n返回结果长度: {len(result)} 字符")

    # 测试 3: 大量输出
    print("\n[Test 3] 大量输出（测试截断）")
    print("-" * 70)

    if platform.system() == "Windows":
        cmd = "dir /s"
    else:
        cmd = "find . -type f"

    result = run_bash(cmd)
    lines = result.count("\n")
    print(f"\n返回结果: {lines} 行")

    print("\n" + "=" * 70)
    print("测试完成！")
    print("=" * 70)

    return 0


if __name__ == "__main__":
    sys.exit(main())
