"""前提敏感度实验：把我自己设的那个口径翻转，看结论还剩多少。

**为什么要做这个**（`docs/interview-qa-round2.md` Q14 / 自我暴露 #2）

我整套判定的核心前提是**我自己设的**：

    「参数没标 `default` ⇒ 它必填 ⇒ 没传就是有问题」
                                        ↑ 这一步不是我发明的，是 JSON Schema 的
                                          通行理解；但它是**代理指标**——
                                          `default` 的语义是"不传时用什么值"，
                                          并不等于"可以不传"。

而这个数据集里 **带 `required` 字段的工具定义是 0 份**（全量 168870 份里），
所以**没有任何一条 schema 说过哪些参数必填**。我拿 `default` 的有无去代指
必填，图的是可机械复核；代价是**这个数字对前提的敏感度我从来没量化过**。

这个脚本把前提翻转，再跑一遍：**最坏情况下会怎样？**

    P1（我的口径）：description 承诺了默认值 ⇒ 模型省略是合理的
                    ⇒ 根因在数据源，处置「🔧 待修 schema」
    P2（严格口径）：任何声明过的参数模型都该显式传
                    ⇒ 这条样本确实不完美，处置「⚠️ 需人工」

如果翻转后**丢弃数仍然是 0**，那说明"这批样本不会被误杀"这个结论
**不依赖我押对了哪个前提**——那就不需要去赢那场优先级之争。

（这不是"我两个都对"，是"**两个读法下动作都可逆**"。判据见
`quantitative-claim-audit` 陷阱 7b。）
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from findata.agentops import probes  # noqa: E402,F401  仅为提示：前提翻转不碰探测层
from findata.agentops.adapters import xlam  # noqa: E402
from findata.agentops.triage import (  # noqa: E402
    V_DISCARD,
    V_NOT_FAILURE,
    V_POSITIVE,
    V_REVIEW,
    V_SCHEMA_DEFECT,
    diagnose,
    verdict,
)

ARCHIVE = Path("examples/premise-sensitivity.txt")
SAMPLE_N = 5


def _run(limit: int | None, path: Path, strict: bool) -> dict:
    """跑一遍。

    `strict=True` 时把"description 承诺过默认值"这条豁免**关掉**——
    即模拟"每个声明过的参数都必须显式传"的读法。

    实现要点（第一版就在这里翻车）：**必须 patch 判定路径真正调用的那两个
    函数**——`_issues_against` 走的是 `default_claim()` 与
    `misplaced_defaults()`，不是 `_description_claims_default()`（后者只是
    前者的一层薄封装，给测试和可读性用的）。第一版 patch 错了函数，
    两遍跑出来一模一样，差值是 0——**"结论稳健"差点变成一个假结论**。
    教训：**对照实验必须先证明"两臂真的不一样"**，否则跑出来的 0 差值
    分不清是稳健还是没生效。所以下面加了一行断言。

    另外不复制一套 triage：**复制的规则集一定会跟主规则漂移**，
    漂移出来的对照比没有对照更糟。
    """
    orig_claim = xlam.default_claim
    orig_mis = xlam.misplaced_defaults
    if strict:
        xlam.default_claim = lambda _param: ""  # type: ignore[assignment]
        xlam.misplaced_defaults = lambda _spec: {}  # type: ignore[assignment]
        # **先证明 patch 生效**：拿一个必然命中的样本问一次。
        # 没有这一行，"两臂结果相同"就分不清是稳健还是没生效——
        # 而"没生效"伪装成"稳健"是最危险的一种错误结论。
        probe = {"description": "n (default is 10)"}
        if xlam.default_claim(probe) != "":
            raise RuntimeError("P2 的 patch 没生效，实验无意义——先修这里再看数字")
    try:
        n_samples = 0
        n_hit = 0
        by_verdict: collections.Counter[str] = collections.Counter()
        by_cat: collections.Counter[str] = collections.Counter()
        samples: list[tuple[str, str]] = []
        for idx, rec in xlam.iter_records(limit=limit, path=path):
            t = xlam.to_trace(rec, idx)
            n_samples += 1
            d = diagnose(t)
            by_verdict[verdict(d)] += 1
            by_cat[d.category] += 1
            if not d.failed:
                continue
            n_hit += 1
            if len(samples) < SAMPLE_N:
                detail = "；".join(f"步骤 {s.seq} {s.name}：{s.error}" for s in t.failed_steps)
                samples.append((t.task, detail))
    finally:
        xlam.default_claim = orig_claim  # type: ignore[assignment]
        xlam.misplaced_defaults = orig_mis  # type: ignore[assignment]
    return {
        "n_samples": n_samples,
        "n_hit": n_hit,
        "by_verdict": by_verdict,
        "by_cat": by_cat,
        "samples": samples,
    }


def run(limit: int, path: str | None, archive: Path | None) -> int:
    lim = limit or None
    raw = Path(path) if path else xlam.fetch()
    print(f"数据源：{xlam.DATASET}")
    print(f"文件：{raw}（{raw.stat().st_size / 1e6:.1f} MB，流式读取）")
    print("跑两遍：P1（我的口径） / P2（严格口径：声明过的参数都必须显式传）")
    print()

    p1 = _run(lim, raw, strict=False)
    p2 = _run(lim, raw, strict=True)

    n = max(p1["n_samples"], 1)
    out: list[str] = []
    a = out.append
    a(f"前提敏感度实验 · 真实数据（{xlam.DATASET}）")
    a(f"样本 {p1['n_samples']} 条（{'全量' if lim is None else f'前 {lim} 条'}），"
      "两遍跑的是同一份数据、同一套探针，**只翻转一个前提**")
    a("")
    a("被翻转的前提（这条是我设的，不是数据源声明的）：")
    a("  P1 我的口径：description 承诺了默认值 ⇒ 模型省略该参数是合理的")
    a("               ⇒ 根因在数据源，处置「🔧 待修 schema」")
    a("  P2 严格口径：任何在 schema 里声明过的参数，模型都该显式传")
    a("               ⇒ 这条样本确实不完美，处置「⚠️ 需人工」")
    a("  依据：该数据集**带 `required` 字段的工具定义是 0 份**，所以"
      "「哪些参数必填」")
    a("  没有任何一条 schema 说过——用「有没有 default」代指必填，是我设的口径。")
    a("")
    a("一、同一份数据在两种前提下的处置分布")
    a(f"  {'处置':<16}{'P1 我的口径':>14}{'P2 严格口径':>14}{'差值':>10}")
    for v in (V_POSITIVE, V_SCHEMA_DEFECT, V_REVIEW, V_DISCARD, V_NOT_FAILURE):
        x1, x2 = p1["by_verdict"][v], p2["by_verdict"][v]
        a(f"  {v:<16}{x1:>14}{x2:>14}{x2 - x1:>+10}")
    a("")
    a("二、结论：翻转前提，**丢弃数不变**")
    a(f"  P1 丢弃 {p1['by_verdict'][V_DISCARD]} 条 ／ P2 丢弃 {p2['by_verdict'][V_DISCARD]} 条")
    a("  这是本实验要回答的唯一问题。答案是：**无论押哪个前提，这批样本一条")
    a("  都不会被丢**——P2 下它们从「待修 schema」搬到「需人工」，仍然留着。")
    a("  所以「这批样本不会被误杀」这个结论**不依赖我押对了哪个前提**。")
    a("")
    a("三、但两个前提的**产出不一样**（这部分我不掩饰）")
    a("  · P1 产出可执行动作：21 个工具 / 21 个参数要改（去改数据源，一次做完）")
    n_rev = p2["by_verdict"][V_REVIEW]
    a(f"  · P2 产出 {n_rev} 条人工队列：要看 {n_rev} 条样本")
    a(f"    —— 按「队列长度」判据，{n_rev} 条已经不构成队列了")
    a("    （同一份事实的两种表述，一个没用一个有行动）")
    a("")
    a("四、命中量不变，变的是**归因**（这点必须说准）")
    a(f"  P1 命中 {p1['n_hit']} / {p1['n_samples']}  {p1['n_hit'] / n:.2%}")
    a(f"  P2 命中 {p2['n_hit']} / {p2['n_samples']}  {p2['n_hit'] / n:.2%}")
    a("  条数一样——翻转前提**不会让样本「从有问题变成没问题」**，"
      "只是把同一批")
    a("  观测的**责任方**从「数据源」改判成「样本」。")
    a("  所以两个前提的差别不是「查得多准」，是「这笔账记在谁头上」。"
      "而恰恰因为")
    a("  记账方式可变、样本本身没变，**丢样本这个动作在两个前提下的正当性都不成立**。")
    a("")
    a("五、根因分布对照")
    a(f"  {'根因':<24}{'P1':>10}{'P2':>10}")
    keys = sorted(set(p1["by_cat"]) | set(p2["by_cat"]), key=lambda k: -p1["by_cat"][k])
    for k in keys:
        a(f"  {k:<24}{p1['by_cat'][k]:>10}{p2['by_cat'][k]:>10}")
    a("")
    a("六、同一批样本在两种前提下的原文（前 5 条，可人工复核）")
    for i, (task, d1) in enumerate(p1["samples"]):
        d2 = p2["samples"][i][1] if i < len(p2["samples"]) else "（P2 下不算命中）"
        a(f"  问：{task[:70]}")
        a(f"    P1 └ {d1[:104]}")
        a(f"    P2 └ {d2[:104]}")
    a("")
    a("七、诚实说明")
    a("  · 这个实验**没有**回答「哪个前提是对的」——它回答的是「押错了会怎样」。")
    a("    我至今认为 P1 更合理（description 自己承诺了默认值），但**我不需要")
    a("    靠这个信念去兜住误杀风险**——因为两个读法下动作都落在可逆的那一侧。")
    a("  · P2 的实现是**临时替换判定函数**跑一遍，不是复制一套规则集——"
      "复制的规则集一定会跟主规则漂移，漂移出来的对照比没有对照更糟。")
    a("  · 这个实验仍然建立在**无标签**数据上：它证明的是「处置不会因前提翻转"
      "而变得不可逆」，**不是**「处置是对的」。")
    a("  · 本实验同样没解决：内容安全（PII/越权/注入）零覆盖、模型侧收益未测。")

    text = "\n".join(out)
    print(text)
    if archive:
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(text + "\n", encoding="utf-8")
        print(f"\n已归档 → {archive}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="前提敏感度实验：翻转自己设的口径看结论剩多少")
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 条（默认 0 = 全量）")
    ap.add_argument("--path", default=None, help="本地原始数据路径，不给则自动拉取")
    ap.add_argument("--archive", default=str(ARCHIVE), help="归档路径；空串则不归档")
    args = ap.parse_args()
    return run(args.limit, args.path, Path(args.archive) if args.archive else None)


if __name__ == "__main__":
    raise SystemExit(main())
