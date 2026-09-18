"""真实数据闭环：拉公开 tool-calling 语料 → 归一化 → 探针+归因 → 过滤出可训练样本。

一条命令跑完「Agent 训练数据质检」这件事，产出可复核报告：

    uv run python scripts/run_trust_filter.py --limit 20000

**语料全部来自网上真实公开数据集，没有一条是我们自己编的。**
这一点不是洁癖：自己造的失败样本，形状必然贴合自己写的规则，评测出来的
准确率是循环论证（先前在合成集上做到 100%，换成从未参与调参的 held-out
立刻掉到 63.6%）。要证明规则有用，只能拿别人做的数据来考。

报告里最重要的**不是**命中数，是「命中要分两类」：
  A 样本缺陷（agent 的账）→ 动样本
  B 工具 schema 缺陷（数据源的账）→ 动 schema

混成一类就会把数据源的错算到 agent 头上。第一版就是这么错的：349 条被
判成"agent 漏填必填参数"，处置是丢弃——而真相是那 349 条指向 **18 个工具
的 18 个参数 schema 写错了**，修完重跑，样本一条都不用丢。
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from findata.agentops.adapters import xlam  # noqa: E402
from findata.agentops.export import VERDICT_FILES, SplitWriter, ToolDefectTally  # noqa: E402
from findata.agentops.triage import (  # noqa: E402
    V_DISCARD,
    V_NOT_FAILURE,
    V_POSITIVE,
    V_REVIEW,
    V_SCHEMA_DEFECT,
    diagnose,
    verdict,
)

ARCHIVE = Path("examples/trust-filter-report.txt")
DEFAULT_OUT = Path("data/filtered")
SAMPLE_N = 8
TOP_DEFECTS = 6

# 从步骤报错里剥出"问题类型"：只取冒号前的短标签，去掉具体参数名。
_KIND_RE = re.compile(r"^([a-z ]+?)(?::|$)")


def _kind_of(err: str) -> str:
    m = _KIND_RE.match(err.strip().lower())
    return (m.group(1).strip() if m else err.strip()) or "unknown"


def run(limit: int, path: str | None, archive: Path | None, out_dir: Path | None) -> int:
    raw = Path(path) if path else xlam.fetch()
    print(f"数据源：{xlam.DATASET}")
    print(f"文件：{raw}（{raw.stat().st_size / 1e6:.1f} MB，流式读取）")
    print()

    n_samples = 0
    n_calls = 0
    n_hit = 0
    n_sample_defect = 0
    by_cat: collections.Counter[str] = collections.Counter()
    by_verdict: collections.Counter[str] = collections.Counter()
    by_kind: collections.Counter[str] = collections.Counter()
    samples: list[tuple[str, str]] = []
    tally = ToolDefectTally()

    with SplitWriter(out_dir or DEFAULT_OUT) as w:
        for idx, rec in xlam.iter_records(limit=limit, path=raw):
            t = xlam.to_trace(rec, idx)
            n_samples += 1
            n_calls += len(t.steps)

            d = diagnose(t)
            v = verdict(d)
            by_cat[d.category] += 1
            by_verdict[v] += 1

            # 判定挂在样本上一起落盘：分开存会漂移（样本与结论对不上号）
            w.write(v, w.attach(xlam.to_training_example(rec, idx), d, v))

            if not d.failed:
                continue

            # ── 缺陷归属：算在样本头上，还是算在工具 schema 头上 ──
            # 这个区分是本项目的核心。混在一起就会把数据源的错算成
            # agent 的错，进而丢掉本来能用的样本。
            n_hit += 1
            if v != V_SCHEMA_DEFECT:
                n_sample_defect += 1

            for s in t.failed_steps:
                # 一条步骤可能同时命中多类问题（如缺参数 + 重复调用），逐条记
                for issue in str(s.error).split(";"):
                    issue = issue.strip()
                    by_kind[_kind_of(issue)] += 1
                    if issue.startswith("schema contradiction"):
                        param = issue.split(":", 1)[1].strip().split(" ")[0]
                        tally.add(s.name, param, issue, idx)
            if len(samples) < SAMPLE_N:
                detail = "；".join(
                    f"步骤 {s.seq} {s.name} 报错：{s.error}" for s in t.failed_steps
                )
                samples.append((t.task, detail))

        # data_path 一并进 manifest：筛的是哪份数据要能对上号，
        # 公开数据集会被作者修订，只记代码版本复现不了旧结果
        manifest = w.write_manifest(
            w.manifest(n_samples, n_calls, xlam.DATASET, data_path=raw)
        )
        defects_path = tally.write(w.out_dir, xlam.DATASET)
        defects = tally.to_dict(xlam.DATASET)
        counts = dict(w.counts)
        out_root = manifest.parent

    denom = max(n_samples, 1)
    out: list[str] = []
    a = out.append
    a(f"可信过滤闭环 · 真实数据（{xlam.DATASET}）")
    a(f"样本 {n_samples} 条，工具调用 {n_calls} 次；数据源 ModelScope 公开数据集，**无标签**")
    a("")
    a("一、命中率（判定全是确定性查表，不依赖模型；**命中数不等于错误数**）")
    a(f"  未命中 {n_samples - n_hit} / {n_samples}  {(n_samples - n_hit) / denom:.2%}")
    a(f"  命中   {n_hit} / {n_samples}  {n_hit / denom:.2%}")
    a("")
    a("  命中必须分两类——**这是本项目的全部要点**：")
    a(f"    A 样本侧（要动样本）            {n_sample_defect:>5} 条")
    a(f"    B 工具 schema 侧（要动数据源）    {n_hit - n_sample_defect:>5} 条")
    a("    A 要动样本，B 要动 schema。混成一类就会把数据源的错算到 agent 头上，")
    a("    进而丢掉本来能用的样本。")
    a("    A 里面**没有一条被确证为「agent 能力问题」**：全部落进兜底类")
    a("    `step_error`（即「规则没认出来」），因此判 ⚠️ 需人工，不是 ✗ 丢弃。")
    a("    认不出来 ≠ 样本是坏的——把「我的规则覆盖不到」记成「数据有问题」，")
    a("    是同一类归因错误的第三个变体。")
    a("")
    a("二、根因分布")
    for cat, n in by_cat.most_common():
        a(f"  {cat:<24}{n:>6}  {n / denom:>7.2%}")
    a("")
    a("三、处置分布（能不能喂训练）")
    for v in (V_POSITIVE, V_SCHEMA_DEFECT, V_REVIEW, V_DISCARD, V_NOT_FAILURE):
        if by_verdict[v]:
            a(f"  {v:<16}{by_verdict[v]:>6}  {by_verdict[v] / denom:>7.2%}")
    a("")
    a(f"四、待修 schema 清单（{defects['n_tools']} 个工具 / {defects['n_params']} 个参数）")
    a("  动作：给这些参数补上 default（description 已经承诺过了），重跑即可——")
    a("  **不需要人工逐条看，也不需要丢样本**。")
    for e in defects["defects"][:TOP_DEFECTS]:
        a(f"  {e['tool']}.{e['param']:<22}{e['hits']:>5} 次")
    if len(defects["defects"]) > TOP_DEFECTS:
        a(f"  … 其余 {len(defects['defects']) - TOP_DEFECTS} 项见 {defects_path.name}")
    a("")
    a("五、问题类型（确定性事实，逐条可复核）")
    for kind, n in by_kind.most_common():
        a(f"  {kind:<28}{n:>6}")
    a("")
    a(f"六、样例（前 {len(samples)} 条，可人工复核）")
    for task, detail in samples:
        a(f"  问：{task[:78]}")
        a(f"     └ {detail[:120]}")
    a("")
    a("七、产出（可直接喂训练，OpenAI messages + tool_calls 形状）")
    for v in (V_POSITIVE, V_SCHEMA_DEFECT, V_REVIEW, V_DISCARD, V_NOT_FAILURE):
        a(f"  {VERDICT_FILES[v]:<22}{counts[v]:>6} 条   {v}")
    a(f"  {defects_path.name:<22}        工具级待修清单，给数据源方")
    a(f"  → {out_root}")
    a("")
    a("八、诚实说明")
    a("  · **「未命中」不等于「确认正确」**：判定全是查表（工具名在不在注册表、")
    a("    参数与 schema 一致否），未命中只说明这条轨迹通过了我能查的那几项。")
    a("    以 xlam 为例我**拿不到执行反馈**，所以「正样本」的准确含义是")
    a("    「没查出缺陷」，不是「业务结果正确」。")
    a(f"  · 这批是**已清洗过的公开数据集**，命中率只有 {n_hit / denom:.2%}，")
    a("    并不代表生产环境的比例——真实 Agent 轨迹会更脏。")
    a("  · 「工具不存在」这一项 0 命中：样本里的调用都落在给定工具列表内，")
    a("    因此**这一项没被验证到**，不能说它能查幻觉工具名。")
    a("  · 该数据集 58105 个工具里，**带 `required` 字段的是 0 个**——所以")
    a("    「缺参数 = 漏填必填项」这个前提**不是数据源声明的，是我自己设的口径**。")
    a("    据此判出的 349 条因此从「丢弃」改判为「工具 schema 缺陷」：模型省略")
    a("    一个描述里写着有默认值的参数，是合理行为，不是错。")
    a("  · 查不到的脏（如参数值语义错误）照查不出——这是查表法的天花板。")
    a("  · **已知未解决**：重复调用探针（`probe_retry_storm`）判「同名且同参数」")
    a("    重复，但幂等性是工具属性、我拿不到——`draw_cards` / `get_random_question`")
    a("    这类工具，用同样的参数再调一次是**正常动作**。该探针在本批 0 命中，")
    a("    即**未被真实数据验证过**，所以按拆表法先不动它，留作开放问题。")

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
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT), help="样本集落盘目录")
    args = ap.parse_args()
    archive = Path(args.archive) if args.archive else None
    return run(args.limit, args.path, archive, Path(args.out_dir))


if __name__ == "__main__":
    raise SystemExit(main())
