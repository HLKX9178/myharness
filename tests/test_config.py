"""配置解析测试。

重点有三：
1. MAX_TOKENS 必须可配置，且非法值必须显式报错而不是静默回退 ——
   否则「设了却没生效」这类问题无法诊断。
2. 配置错误必须**可操作**：说清缺什么、去哪补。
3. 首次部署（没有 .env）不能甩 traceback。
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from myharness import hooks
from myharness.config import (
    DEFAULT_MAX_TOKENS,
    MAX_TOKENS,
    REPO_ROOT,
    ConfigError,
    _env_dir,
    _env_int,
    _require_env,
)


# ============================================================
# _env_int 的解析规则
# ============================================================

def test_env_int_uses_default_when_unset(monkeypatch):
    monkeypatch.delenv("MYHARNESS_TEST_INT", raising=False)
    assert _env_int("MYHARNESS_TEST_INT", 8000) == 8000


def test_env_int_treats_blank_as_unset(monkeypatch):
    monkeypatch.setenv("MYHARNESS_TEST_INT", "   ")
    assert _env_int("MYHARNESS_TEST_INT", 8000) == 8000


def test_env_int_parses_value(monkeypatch):
    monkeypatch.setenv("MYHARNESS_TEST_INT", "16000")
    assert _env_int("MYHARNESS_TEST_INT", 8000) == 16000


def test_env_int_tolerates_surrounding_whitespace(monkeypatch):
    monkeypatch.setenv("MYHARNESS_TEST_INT", " 4096 ")
    assert _env_int("MYHARNESS_TEST_INT", 8000) == 4096


def test_env_int_rejects_non_integer(monkeypatch):
    monkeypatch.setenv("MYHARNESS_TEST_INT", "8k")
    with pytest.raises(ConfigError, match="必须是整数"):
        _env_int("MYHARNESS_TEST_INT", 8000)


def test_env_int_rejects_below_minimum(monkeypatch):
    monkeypatch.setenv("MYHARNESS_TEST_INT", "0")
    with pytest.raises(ConfigError, match="必须 >= 1"):
        _env_int("MYHARNESS_TEST_INT", 8000)


def test_env_int_rejects_negative(monkeypatch):
    monkeypatch.setenv("MYHARNESS_TEST_INT", "-1")
    with pytest.raises(ConfigError, match="必须 >= 1"):
        _env_int("MYHARNESS_TEST_INT", 8000)


def test_error_message_tells_how_to_fix(monkeypatch):
    """错误信息必须包含「如何修正」：变量名 + 合法示例。"""
    monkeypatch.setenv("MYHARNESS_TEST_INT", "abc")
    with pytest.raises(ConfigError) as excinfo:
        _env_int("MYHARNESS_TEST_INT", 8000)
    message = str(excinfo.value)
    assert "MYHARNESS_TEST_INT" in message
    assert ".env" in message
    assert "8000" in message


def test_config_error_is_a_value_error():
    """ConfigError 继承 ValueError —— 配置取值不对本来就是 ValueError。

    这样既能让 __main__ 单独识别并渲染提示，又不会让「本来就是
    ValueError」的调用方突然接不住。
    """
    assert issubclass(ConfigError, ValueError)


# ============================================================
# _require_env：必填项的报错必须可操作
# ============================================================

def test_require_env_returns_stripped_value(monkeypatch):
    monkeypatch.setenv("MYHARNESS_TEST_REQ", "  value  ")
    assert _require_env("MYHARNESS_TEST_REQ", "用途说明") == "value"


def test_require_env_rejects_unset(monkeypatch):
    monkeypatch.delenv("MYHARNESS_TEST_REQ", raising=False)
    with pytest.raises(ConfigError, match="MYHARNESS_TEST_REQ"):
        _require_env("MYHARNESS_TEST_REQ", "用途说明")


def test_require_env_treats_blank_as_unset(monkeypatch):
    """`MODEL_ID=`（值为空）与完全没写同等对待。"""
    monkeypatch.setenv("MYHARNESS_TEST_REQ", "   ")
    with pytest.raises(ConfigError, match="未设置"):
        _require_env("MYHARNESS_TEST_REQ", "用途说明")


def test_require_env_message_says_where_to_put_it(monkeypatch):
    """缺失必填项时，信息里必须带「怎么补」—— 这是首次部署唯一需要知道的事。"""
    monkeypatch.delenv("MYHARNESS_TEST_REQ", raising=False)
    with pytest.raises(ConfigError) as excinfo:
        _require_env("MYHARNESS_TEST_REQ", "它是模型的名字")
    message = str(excinfo.value)
    assert "MYHARNESS_TEST_REQ" in message
    assert "它是模型的名字" in message
    assert ".env.example" in message      # 从哪复制模板
    assert "cp .env.example .env" in message


# ============================================================
# 默认值本身
# ============================================================

def test_default_max_tokens_is_positive():
    assert isinstance(DEFAULT_MAX_TOKENS, int)
    assert DEFAULT_MAX_TOKENS > 0


def test_max_tokens_is_configured():
    """MAX_TOKENS 必须已经解析成整数，且不小于 1。"""
    assert isinstance(MAX_TOKENS, int)
    assert MAX_TOKENS >= 1


# ============================================================
# loop 必须消费这个配置，而不是自己硬编码
# ============================================================

def test_agent_loop_passes_configured_max_tokens(monkeypatch):
    """agent_loop 调用 provider.chat 时使用的必须是 config.MAX_TOKENS。

    这是防止「改回硬编码」的回归测试。
    """
    from myharness import config, loop

    captured = {}

    class FakeProvider:
        def chat(self, messages, tools, system, max_tokens, on_text=None):
            captured["max_tokens"] = max_tokens
            # 返回空 content：没有 tool_use，循环会走到 Stop 后退出
            return SimpleNamespace(
                content=[],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1),
            )

    hooks.clear_hooks()
    try:
        monkeypatch.setattr(loop, "provider", FakeProvider())
        loop.agent_loop([{"role": "user", "content": "hello"}])
    finally:
        hooks.clear_hooks()

    assert captured["max_tokens"] == config.MAX_TOKENS


# ============================================================
# WORKDIR：让 Agent 在别的目录工作
# ============================================================

def test_env_dir_uses_default_when_unset(monkeypatch, tmp_path):
    monkeypatch.delenv("MYHARNESS_TEST_DIR", raising=False)
    assert _env_dir("MYHARNESS_TEST_DIR", tmp_path) == tmp_path


def test_env_dir_reads_existing_directory(monkeypatch, tmp_path):
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setenv("MYHARNESS_TEST_DIR", str(target))
    assert _env_dir("MYHARNESS_TEST_DIR", tmp_path) == target.resolve()


def test_env_dir_rejects_missing_directory(monkeypatch, tmp_path):
    """目录不存在就报错，否则会被静默接受成一个写不进去的工作区。"""
    monkeypatch.setenv("MYHARNESS_TEST_DIR", str(tmp_path / "nope"))
    with pytest.raises(ConfigError, match="已存在的目录"):
        _env_dir("MYHARNESS_TEST_DIR", tmp_path)


def test_workdir_env_var_is_honoured(tmp_path):
    """端到端：WORKDIR 环境变量必须让 Agent 在另一个目录工作。

    必须在子进程里跑 —— config.WORKDIR 在 import 时求值，同进程内改环境
    变量已经太晚（这正是 teams 隔离一度失效的根因）。
    """
    target = tmp_path / "other_project"
    target.mkdir()

    env = {
        **os.environ,
        "WORKDIR": str(target),
        "PYTHONPATH": str(REPO_ROOT),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import myharness.config as c; print('WORKDIR=' + str(c.WORKDIR))",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(REPO_ROOT),
    )

    line = next(l for l in proc.stdout.splitlines() if l.startswith("WORKDIR="))
    assert Path(line.split("=", 1)[1]) == target.resolve()


# ============================================================
# 首次部署：没有 .env 时必须给提示，不能甩 traceback
# ============================================================

def test_missing_env_is_reported_without_traceback(tmp_path):
    """新机器 clone 完忘了建 .env，拿到的应该是提示而不是回溯。

    这是真实踩过的坑：裸 `KeyError: 'MODEL_ID'` 只说少了这一项，不说该在哪填、
    填成什么样 —— 而这正是新用户唯一需要知道的事。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copytree(REPO_ROOT / "myharness", repo / "myharness")

    # 清掉可能从当前环境继承来的配置，确保走的是「什么都没有」这条路径
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PROVIDER", "MODEL_ID", "OPENAI_", "ANTHROPIC_"))
    }
    env["PYTHONPATH"] = str(repo)

    proc = subprocess.run(
        [sys.executable, "-m", "myharness"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        cwd=str(repo),
        stdin=subprocess.DEVNULL,
        timeout=180,
    )

    output = proc.stdout + proc.stderr
    assert "Traceback" not in output, output
    assert "配置错误" in output
    assert "MODEL_ID" in output
    assert ".env.example" in output      # 告诉人模板在哪、怎么复制
    assert proc.returncode == 2          # 非 0 且与「崩溃(1)」区分开
