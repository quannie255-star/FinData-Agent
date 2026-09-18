"""真实数据闭环：拉公开 tool-calling 语料 → 归一化 → 探针+归因 → 过滤出可训练样本。

一条命令跑完「Agent 训练数据质检」这件事，产出可复核报告：

    uv run python scripts/run_trust_filter.py --limit 20000

**语料全部来自网上真实公开数据集，没有一条是我们自己编的。**
这一点不是洁癖：自己造的失败样本，形状必然贴合自己写的规则，评测出来的
准确率是循环论证（先前在合成集上做到 100%，换成从未参与调参的 held-out
立刻掉到 63.6%）。要证明规则有用，只能拿别人做的数据来考。

报告里刻意保留「诚实说明」一节：检出率多少、哪些类别一次都没命中
（没命中 = 没被验证到，不能算它works）、以及查不出的脏有哪些。
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from findata.agentops.adapters import xlam  # noqa: E402
from findata.agentops.triage import (  # noqa: E402
    V_DISCARD,
    V_NOT_FAILURE,
    V_POSITIVE,
    V_REVIEW,
    diagnose,
    verdict,
)

ARCHIVE = Path("examples/trust-filter-report.txt")
SAMPLE_N = 8

# 从步骤报错里剥出"问题类型"：只取冒号前的短标签，去掉具体参数名。
_KIND_RE = re.compile(r"^([a-z ]+?)(?::|$)")


def _kind_of(err: str) -> str:
    m = _KIND_RE.match(err.strip().lower())
    return (m.group(1).strip() if m else err.strip()) or "unknown"


def run(limit: int, path: str | None, archive: Path | None) -> int:
    raw = Path(path) if path else xlam.fetch()
    print(f"数据源：{xlam.DATASET}")
    print(f"文件：{raw}（{raw.stat().st_size / 1e6:.1f} MB，流式读取）")
    print()

    n_samples = 0
    n_calls = 0
    by_cat: collections.Counter[str] = collections.Counter()
    by_verdict: collections.Counter[str] = collections.Counter()
    by_kind: collections.Counter[str] = collections.Counter()
    samples: list[tuple[str, str]] = []
    n_bad = 0

    for idx, rec in xlam.iter_records(limit=limit, path=raw):
        t = xlam.to_trace(rec, idx)
        n_samples += 1
        n_calls += len(t.steps)

        d = diagnose(t)
        by_cat[d.category] += 1
        by_verdict[verdict(d)] += 1
        if not d.failed:
            continue

        n_bad += 1
        for s in t.failed_steps:
            # 一条步骤可能同时命中多类问题（如缺参数 + 重复调用），逐条记
            for issue in str(s.error).split(";"):
                by_kind[_kind_of(issue)] += 1
        if len(samples) < SAMPLE_N:
            detail = "；".join(
                f"步骤 {s.seq} {s.name} 报错：{s.error}" for s in t.failed_steps
            )
            samples.append((t.task, detail))

    out: list[str] = []
    a = out.append
    a(f"可信过滤闭环 · 真实数据（{xlam.DATASET}）")
    a(f"样本 {n_samples} 条，工具调用 {n_calls} 次；数据源 ModelScope 公开数据集，**无标签**")
    a("")
    a("一、发现率（不需要 ground truth：判定全是确定性查表）")
    a(f"  有问题 {n_bad} / {n_samples}   {n_bad / max(n_samples, 1):.2%}")
    a(f"  干净   {n_samples - n_bad} / {n_samples}  {(n_samples - n_bad) / max(n_samples, 1):.2%}")
    a("")
    a("二、根因分布")
    for cat, n in by_cat.most_common():
        a(f"  {cat:<24}{n:>6}  {n / max(n_samples, 1):>7.2%}")
    a("")
    a("三、处置分布（能不能喂训练）")
    for v in (V_POSITIVE, V_REVIEW, V_DISCARD, V_NOT_FAILURE):
        if by_verdict[v]:
            a(f"  {v:<16}{by_verdict[v]:>6}  {by_verdict[v] / max(n_samples, 1):>7.2%}")
    a("")
    a("四、问题类型（确定性事实，逐条可复核）")
    for kind, n in by_kind.most_common():
        a(f"  {kind:<28}{n:>6}")
    a("")
    a(f"五、样例（前 {len(samples)} 条，可人工复核）")
    for task, detail in samples:
        a(f"  问：{task[:78]}")
        a(f"     └ {detail[:120]}")
    a("")
    a("六、诚实说明")
    a("  · 这批是**已清洗过的公开数据集**，脏率只有 "
      f"{n_bad / max(n_samples, 1):.2%}，")
    a("    并不代表生产环境的比例——真实 Agent 轨迹会更脏。")
    a("  · 本数据集在「工具不存在」这一项上 0 命中：样本里的调用都落在")
    a("    给定工具列表内，因此**这一项没被验证到**，不能说它能查幻觉工具名。")
    a("  · 判定全是查表（工具名 ∈ 列表？必填参数有没有？），不依赖模型，")
    a("    所以没有准确率问题；但**查不到的脏**（如参数值语义错误）也照查不出。")
    a("  · 「丢弃」不等于归责 agent：`schema contradiction` 是数据源标注")
    a("    自相矛盾，但要剔除是因为它会教模型省略必填参数——根因与处置")
    a("    是两件事，混成一维就没法分别调。")

    text = "\n".join(out)
    print(text)
    if archive:
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(text + "\n", encoding="utf-8")
        print(f"\n已归档 → {archive}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="真实 Agent 轨迹质检闭环")
    ap.add_argument("--limit", type=int, default=20000, help="只取前 N 条（默认 20000）")
    ap.add_argument("--path", default=None, help="本地原始数据路径，不给则自动拉取")
    ap.add_argument("--archive", default=str(ARCHIVE), help="报告归档路径；空串则不归档")
    args = ap.parse_args()
    archive = Path(args.archive) if args.archive else None
    return run(args.limit, args.path, archive)


if __name__ == "__main__":
    raise SystemExit(main())
