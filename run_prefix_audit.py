"""前缀稳定性审计：查清「缓存为什么没命中」。

背景
----
任何前缀缓存的命中条件都只有一条：**本次请求的开头与上次逐字节相同**。
一次请求的前缀顺序是固定的：

    [tools] → [system] → [messages...]

system 在 messages 之前，所以 system 里变一个字节，后面全部历史一起作废。

这个脚本把前缀拆成三段分别取指纹，**离线**指出是哪一段在变，零 API 调用：

    python run_prefix_audit.py

加 --live 再做三次真实调用，用来回答一个离线查不出来的问题：
**这个网关到底上不上报 cached_tokens？**（不上报不等于没缓存，只是看不见。）

    python run_prefix_audit.py --live
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from difflib import unified_diff

from myharness.config import build_system_prompt, get_workdir
from myharness.tools import assemble_tool_pool

# system 里不允许出现的动态内容标记：记忆块曾经被拼在这里。
# 现在记忆走 UserPromptSubmit 注入用户消息，system 必须保持逐字节稳定。
MEMORY_MARKERS = ("## Long-term Memory", "for reference only — if they conflict")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _section(title: str) -> None:
    print(f"\n{title}")
    print("-" * 68)


def audit_tools() -> bool:
    """两次组装工具池，比较结果是否逐字节一致。"""
    _section("[1] tools —— 每次请求都在最前面，顺序一抖全段失效")

    runs = []
    for _ in range(2):
        tools, _handlers = assemble_tool_pool(get_workdir())
        # 用 name + schema 的规范化 JSON 取指纹：比 dict 相等更严格，
        # 键序变了也算变化（大多数网关按字节比前缀）。
        payload = json.dumps(tools, sort_keys=True, ensure_ascii=False)
        runs.append(([t["name"] for t in tools], payload))

    names_a, payload_a = runs[0]
    names_b, payload_b = runs[1]

    print(f"    工具数: {len(names_a)}    指纹: {_digest(payload_a)} / {_digest(payload_b)}")

    stable = payload_a == payload_b
    if stable:
        print("    结论: 两次组装完全一致")
    else:
        print("    结论: 不一致 —— 工具池顺序或内容在两次调用间变了")
        if names_a != names_b:
            print(f"    顺序差异: {names_a} vs {names_b}")
    return stable


def audit_system() -> bool:
    """两次构建 system，并确认里面没有随提问变化的记忆块。

    `build_system_prompt()` 已经不接收用户输入，所以「换一个问题」这条测试
    在结构上就不成立了；真正值得盯的是有没有人把动态内容又塞回 system
    （记忆、时间戳、随机排序）。这里同时检查字节稳定性与记忆标记。
    """
    _section("[2] system —— OpenAI 兼容端点会把它拼成 messages[0]")

    system_a = build_system_prompt(get_workdir())
    system_b = build_system_prompt(get_workdir())

    print(f"    长度: {len(system_a):,} 字符")
    print(f"    指纹: {_digest(system_a)} / {_digest(system_b)}")

    if system_a != system_b:
        print("    结论: 两次构建不一致 —— system 里有随调用变化的内容")
        diff = list(
            unified_diff(
                system_a.splitlines(),
                system_b.splitlines(),
                fromfile="system #1",
                tofile="system #2",
                lineterm="",
                n=0,
            )
        )
        for line in diff[2:12]:
            print(f"      {line}")
        if len(diff) > 12:
            print(f"      ... 另有 {len(diff) - 12} 行差异")
        return False

    leaked = [marker for marker in MEMORY_MARKERS if marker in system_a]
    if leaked:
        print(f"    结论: 字节稳定，但 system 里仍有记忆块 {leaked}")
        print("          记忆应注入用户消息（UserPromptSubmit），不要放进 system")
        return False

    print("    结论: 字节稳定，且不含记忆块（记忆由 UserPromptSubmit 注入用户消息）")
    return True


def audit_conclusion(tools_stable: bool, system_stable: bool) -> None:
    _section("[3] 结论")

    if tools_stable and system_stable:
        print("    前缀三段都稳定，缓存只受上下文压缩影响（那是设计使然）。")
        print("    下一步：跑 --live，确认网关是否上报 cached_tokens。")
        return

    print("    前缀在请求开头就分叉了。因为 tools 与 system 都在 messages 之前，")
    print("    失效的不是它们自己那几百 token，而是**之后全部历史**——")
    print("    对话越长，单次作废的代价越大。\n")

    if not tools_stable:
        print("    待修: tools 顺序不稳。组装后按 name 排序即可。")
    if not system_stable:
        print("    待修: system 里有动态内容或记忆块。它们属于用户消息，不该进 system。")


# ---------------------------------------------------------------------------
# 可选：三次真实调用，判断网关是否上报缓存命中
# ---------------------------------------------------------------------------

# 需要足够长才能触发缓存：多数网关要求前缀 ≥1024 token，低于这条线时
# 「没命中」和「没缓存」无法区分。40 行只有约 900 token，不够。
MIN_CACHEABLE_TOKENS = 1024

FILLER = "".join(
    f"第{i}行：这是一段用于验证前缀缓存行为的占位文本，不含任何业务含义。\n"
    for i in range(1, 141)
)


def _ask(provider, messages: list[dict], system: str = "") -> tuple[int, int]:
    """发一次极短的请求，返回 (input_tokens, cached_tokens)。"""
    response = provider.chat(
        messages=messages,
        tools=[],
        system=system or "你是一个只回复 ok 的测试端点。",
        max_tokens=8,
    )
    usage = response.usage
    return usage.input_tokens, getattr(usage, "cached_tokens", 0)


def live_probe() -> None:
    _section("[4] 真实调用（--live）—— 网关是否上报 cached_tokens")

    from myharness.config import provider  # 延迟导入：离线审计不需要凭据

    first = [{"role": "user", "content": FILLER}]
    grown = [*first, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "继续"}]

    print("    第 1 次：新前缀（若 5 分钟内跑过本脚本，可能直接命中上次的缓存）")
    in1, hit1 = _ask(provider, first)
    print(f"      input={in1:,}  cached={hit1:,}")

    print("    第 2 次：在相同前缀后追加，预期命中")
    in2, hit2 = _ask(provider, grown)
    print(f"      input={in2:,}  cached={hit2:,}")

    print("    第 3 次：与第 2 次逐字节相同，这是最强的命中条件")
    in3, hit3 = _ask(provider, grown)
    print(f"      input={in3:,}  cached={hit3:,}")

    print()
    if hit3 or hit2 or hit1:
        print("    网关会上报缓存命中。之后看 loop 里的 [tokens] cache 一栏即可。")
    elif max(in1, in2, in3) < MIN_CACHEABLE_TOKENS:
        print(f"    样本只有 {max(in1, in2, in3):,} token，低于 {MIN_CACHEABLE_TOKENS} 的常见门槛。")
        print("    这个结果**不能**说明网关不支持缓存——加大 FILLER 后重跑。")
        return
    else:
        print("    三次都是 0，且样本超过门槛。第 3 次与第 2 次逐字节相同却没命中，")
        print("    说明**不是前缀在变**，而是这个网关不上报该字段，或该模型未开启上下文缓存。")
        print("    影响：命中率无法从 usage 观测，只能靠离线审计（本脚本前两节）保证前缀稳定。")
        return

    _live_counterfactual(provider, grown)


def _live_counterfactual(provider, grown: list[dict]) -> None:
    """对照组：只把 system 改一行，看命中是否归零。

    这是整个脚本最值钱的一次调用 —— 它把「system 变一字节 = 整段历史作废」
    从推理变成实测。
    """
    _section("[5] 对照：system 只改一行，命中会怎样")

    baseline = "你是一个只回复 ok 的测试端点。"
    changed = baseline + "\n- [project] 这是一条新写入的记忆。"

    in_a, hit_a = _ask(provider, grown, system=baseline)
    print(f"    system 不变: input={in_a:,}  cached={hit_a:,}")

    in_b, hit_b = _ask(provider, grown, system=changed)
    print(f"    system 改一行: input={in_b:,}  cached={hit_b:,}")

    print()
    if hit_a and not hit_b:
        dropped = in_b - hit_b
        print(f"    命中从 {hit_a:,} 掉到 0。这一轮多付了约 {dropped:,} token 的全价。")
        print("    对话越长，这个数字越大 —— 因为作废的是 messages，不只是 system。")
    elif not hit_a:
        print("    基线本身没命中，对照无效（可能缓存已过期，重跑即可）。")
    else:
        print("    两次都命中，说明该网关的缓存不包含 system 段 —— 与常见实现不同，值得记录。")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="审计请求前缀的稳定性")
    parser.add_argument(
        "--live",
        action="store_true",
        help="额外做三次真实调用，判断网关是否上报 cached_tokens（有费用，极低）",
    )
    args = parser.parse_args(argv)

    print("=" * 68)
    print("前缀稳定性审计")
    print("=" * 68)

    tools_stable = audit_tools()
    system_stable = audit_system()
    audit_conclusion(tools_stable, system_stable)

    if args.live:
        live_probe()
    else:
        print("\n    （加 --live 可验证网关是否上报缓存命中）")

    return 0


if __name__ == "__main__":
    sys.exit(main())
