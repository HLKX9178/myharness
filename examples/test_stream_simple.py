#!/usr/bin/env python3
"""简单测试流式输出。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from myharness.tools import run_bash

print("测试 1: 快速命令")
result = run_bash("echo Step 1 && echo Step 2 && echo Step 3")
print(f"Result: {repr(result[:100])}")

print("\n测试 2: 列出文件")
result = run_bash("ls -la")
print(f"Lines: {result.count(chr(10))}")

print("\n完成！")
