"""system prompt 必须反映本次会话真实的 workspace。

回归目标：``agent_loop(messages, workspace=...)`` 只把 workspace 传给了工具池
和 dispatch，system prompt 却还在用全局 ``config.WORKDIR`` 拼路径。

TeamAgent 传给每个 worker 的是 ``.team/<agent>/workspace``，模型却被告知
「你在仓库根」—— 于是它用相对路径去操作仓库根的文件，被工具层的 safe_path
一条条拒绝，表现为连续重试同一批失败调用。

不变量：**system prompt 里出现的路径，就是 agent_loop 真正在用的 workspace。**
"""

from types import SimpleNamespace

import pytest

from myharness import config, loop


class _RecordingProvider:
    """记录本次请求的 system prompt，返回一句纯文本结束循环。"""

    def __init__(self):
        self.system: str | None = None
        self.calls = 0

    def chat(self, messages, system=None, **kwargs):
        self.system = system
        self.calls += 1
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )


@pytest.fixture
def provider(monkeypatch):
    recorder = _RecordingProvider()
    monkeypatch.setattr(loop, "provider", recorder)
    return recorder


@pytest.fixture(autouse=True)
def clean_hooks():
    from myharness import hooks

    hooks.clear_hooks()
    yield
    hooks.clear_hooks()


def test_agent_loop_prompt_uses_given_workspace(tmp_path, provider):
    """回归：显式传入的 workspace 必须出现在 system prompt 里。"""
    agent_ws = tmp_path / ".team" / "agent_0" / "workspace"
    agent_ws.mkdir(parents=True)

    loop.agent_loop([{"role": "user", "content": "hi"}], workspace=agent_ws)

    assert str(agent_ws) in provider.system
    assert str(config.get_workdir()) not in provider.system, (
        "system prompt 仍在告诉模型全局工作目录"
    )


def test_agent_loop_prompt_defaults_to_global_workdir(provider):
    """不传 workspace 时行为不变：仍然指向全局工作目录。"""
    loop.agent_loop([{"role": "user", "content": "hi"}])

    assert str(config.get_workdir()) in provider.system


def test_build_system_prompt_accepts_workspace(tmp_path):
    """底层函数自身也要支持显式 workspace，而不是只有 loop 能传。"""
    prompt = config.build_system_prompt(workspace=tmp_path)

    assert f"You are a coding agent at {tmp_path}" in prompt


def test_two_workers_get_different_prompts(tmp_path, monkeypatch):
    """同一进程里两个 agent 各自开工时，prompt 不能互相串味。"""
    prompts: dict[str, str] = {}

    for index in range(2):
        workspace = tmp_path / ".team" / f"agent_{index}" / "workspace"
        workspace.mkdir(parents=True)

        recorder = _RecordingProvider()
        monkeypatch.setattr(loop, "provider", recorder)
        loop.agent_loop([{"role": "user", "content": "hi"}], workspace=workspace)

        prompts[f"agent_{index}"] = recorder.system

    assert str(tmp_path / ".team" / "agent_0" / "workspace") in prompts["agent_0"]
    assert str(tmp_path / ".team" / "agent_1" / "workspace") in prompts["agent_1"]
    assert str(tmp_path / ".team" / "agent_1" / "workspace") not in prompts["agent_0"]
