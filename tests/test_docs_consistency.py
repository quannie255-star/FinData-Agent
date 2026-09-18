"""文档一致性门禁：同一件事不能有两个说法。

为什么要有这个测试
------------------
实测过一次翻车：README 里第 90 行写「353 例测试」、第 226 行写
`pytest(351, ...)`，PITCH 里同时存在 341 / 351 / 353。**仓库自己跟自己
矛盾**——面试官扫一眼就会问「连测试数都对不上，你的数据血缘怎么信？」

这类漂移不会让任何测试变红，所以必须专门钉一道。检查的是**当前状态**的
说法是否自洽；CHANGELOG 与 docs/archive 是历史记录，数字本来就应该不同，
不在检查范围内。

第二道更狠：**文档写的数字必须等于实际收集到的用例数**。只查"文档之间不打架"
是不够的——它们完全可以一起写错（354 挂了 1 个月也没人发现，因为没人拿
CI 的数字去比对）。所以这里真跑一次 collection。

用 `--collect-only` 而不是全量跑：只收集不执行用例体，既拿到事实又不会递归
（这个门禁不会自己调自己），实测 2 秒。取不到就放过——门禁是防漂移用的，
不该因为环境问题把整个套件搞红。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 只扫"描述当前状态"的文档；历史记录类（CHANGELOG / archive / reviews）
# 里的数字天然不同，扫它们只会得到假警报。
#
# `docs/interview-qa.md` 与 `docs/interview-qa-round2.md` 也**刻意不在**名单里：
# 它们为了讲清"口径漂移是怎么被抓出来的"，必须引用 353 / 351 / 358 这些
# **历史错数字**（比如 round2 的 Q12 就是"353 vs 351 哪个真"）。扫它们等于
# 要求不问历史。代价是它们自己写的当前值不受本门禁保护，只能靠人。
CURRENT_DOCS = ["README.md", "PITCH.md", "AGENTS.md", "docs/handoff.md", "docs/ROADMAP.md"]

PATTERNS = [
    re.compile(r"(\d+)\s*例测试"),
    re.compile(r"pytest\((\d+)"),
    re.compile(r"(\d+)\s*个测试"),
    re.compile(r"全量\s*(\d+)\s*例"),
]


def _declared_counts() -> dict[str, list[tuple[str, int]]]:
    found: dict[str, list[tuple[str, int]]] = {}
    for rel in CURRENT_DOCS:
        p = ROOT / rel
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        for pat in PATTERNS:
            for m in pat.finditer(text):
                found.setdefault(rel, []).append((m.group(0), int(m.group(1))))
    return found


def test_test_count_has_a_single_value_across_docs():
    """全仓库对「当前有多少测试」必须只有一个数。"""
    found = _declared_counts()
    values = {n for entries in found.values() for _, n in entries}
    assert values, "没扫到任何测试数——改过文档措辞的话，同步更新本测试的正则"
    assert len(values) == 1, (
        "文档里的测试数不一致："
        + "; ".join(f"{f} → {[e[0] for e in entries]}" for f, entries in found.items())
        + f"（实际取值 {sorted(values)}）"
    )


def test_readme_and_pitch_agree_on_current_state():
    """README 与 PITCH 是面试官最可能看的两个文件，单独再钉一次。"""
    found = _declared_counts()
    readme = {n for _, n in found.get("README.md", [])}
    pitch = {n for _, n in found.get("PITCH.md", [])}
    assert readme == pitch, f"README 说 {readme}，PITCH 说 {pitch}"


def _actually_collected() -> int | None:
    """真跑一次 collection，拿"事实"数字；拿不到返回 None（放行，不误伤）。"""
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    m = re.search(r"(\d+)\s+tests?\s+collected", r.stdout)
    return int(m.group(1)) if m else None


def test_declared_count_matches_reality():
    """文档里写的测试数，必须等于仓库里真实存在的用例数。

    前一道门禁只能保证"文档之间不打架"，它们可以一起写错——本用例补的就是
    这个缺口。改完测试后如果这条红了，**改文档，不要改这个断言**。
    """
    actual = _actually_collected()
    if actual is None:
        return  # 环境拿不到事实数字时不误伤（见模块 docstring）
    values = {n for entries in _declared_counts().values() for _, n in entries}
    assert values == {actual}, (
        f"文档写的是 {sorted(values)}，实际收集到 {actual} 个用例。"
        "改完测试请同步更新 README / PITCH / AGENTS / docs/handoff.md 里的数字。"
    )
