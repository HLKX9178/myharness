"""`run.bat` 的契约测试。

这个文件没法像 Python 模块那样直接跑单元测试，但它有两条容易在改动中
悄悄破坏、且破坏后症状难查的不变量：

1. **必须纯 ASCII。** cmd.exe 按系统 OEM 代码页读取 .bat，混入非 ASCII 字符
   会让括号块的解析出错 —— 这条原先只写在文件注释里，没有任何东西拦得住。
2. **依赖探测列表必须覆盖源码真正 import 的第三方包。** 探测列表是手写的，
   新增依赖时很容易漏改，而漏改的后果是「依赖明明装了，run.bat 仍拒绝启动」。

第 2 条刻意不维护一份重复清单，而是直接从 `myharness/` 的 AST 里算出第三方
import，再与 run.bat 探测的模块比对 —— 这样它自己不会过期。
"""

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RUN_BAT = REPO_ROOT / "run.bat"

# 探测语句形如：python.exe -c "import dotenv, openai, anthropic, croniter"
_PROBE_RE = re.compile(r'-c\s+"import\s+([^"]+)"')


def _probed_modules() -> set[str]:
    """解析 run.bat 依赖探测那行里 import 的模块名。"""
    match = _PROBE_RE.search(RUN_BAT.read_text(encoding="utf-8"))
    assert match, "run.bat 里找不到依赖探测语句"
    return {name.strip() for name in match.group(1).split(",") if name.strip()}


def _third_party_imports() -> set[str]:
    """扫描 `myharness/` 的顶层 import，算出真正的第三方依赖。

    过滤掉标准库与本包自身。平台特有的标准库模块（fcntl / msvcrt）同样出现在
    `sys.stdlib_module_names` 里，所以不需要额外的白名单。
    """
    found: set[str] = set()
    for path in (REPO_ROOT / "myharness").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                found.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return {name for name in found if name not in sys.stdlib_module_names and name != "myharness"}


def test_run_bat_is_ascii_only():
    """非 ASCII 字符会让 cmd.exe 解析括号块出错，必须挡住。"""
    raw = RUN_BAT.read_bytes()
    offenders = sorted({byte for byte in raw if byte > 127})
    assert not offenders, f"run.bat 混入了非 ASCII 字节: {offenders}"


def test_probe_covers_every_third_party_import():
    """漏改探测列表的后果是「依赖装了仍被拒绝启动」，所以两者必须一致。"""
    missing = _third_party_imports() - _probed_modules()
    assert not missing, f"这些第三方依赖没有被 run.bat 探测: {sorted(missing)}"


def test_probe_does_not_require_dev_dependencies():
    """pytest 是开发依赖，启动路径不该依赖它。"""
    assert "pytest" not in _probed_modules()


def test_python_is_checked_before_dependencies_and_launch():
    """顺序有意义：没有解释器时不该谈依赖，而探测必须发生在真正启动之前。"""
    text = RUN_BAT.read_text(encoding="utf-8")
    venv_check = text.index("if not exist")
    probe = _PROBE_RE.search(text).start()
    launch = text.index("-m myharness")
    assert venv_check < probe < launch
