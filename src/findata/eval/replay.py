"""真实回放评测（R1.2 / 复盘 P0c）：用固化的真实仓库快照重放 2026-09-15 巡检。

合成语料证明「机制对」，真实回放证明「事实够」，两者缺一不可。
数据来自 `eval/fixtures/replay_20260915/*.parquet`（生产仓库只读固化，
见 scripts/build_replay_fixture.py）；corporate_event 由停牌种子确定性
重建（种子是本笔评测钉死的知识层事实，仓库事件表会随采集漂移）。

回放纪律：`Snapshot.as_of(asof)` 截断——用今天的全量数据去解释
当时的告警没有意义，且会引入未来数据导致结论失真。
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from findata.core.db import connect
from findata.domains.finance.seeds import apply_suspension_seeds
from findata.dq.badges import BadgeLevel
from findata.report.inspect import Snapshot, run_inspection

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = REPO_ROOT / "eval" / "fixtures" / "replay_20260915"
GOLDEN_PATH = REPO_ROOT / "eval" / "golden" / "replay.yaml"

_TABLES = ("stock_daily", "valuation_daily", "index_daily", "stock_universe", "trading_calendar")


@dataclass
class ReplayCaseResult:
    name: str
    ok: bool
    detail: str


def load_replay_snapshot(fixture_dir: Path = FIXTURE_DIR) -> Snapshot:
    """从 parquet fixture 装载回放快照（含种子重建的知识层）。

    用显式的内存库承载 fixture——绝不碰 settings.dsn 指向的生产仓库，
    这是 build_eval_fixture 事故（v0.7.1）立下的规矩。
    """
    if not fixture_dir.exists():
        raise FileNotFoundError(
            f"回放 fixture 不存在：{fixture_dir}。先跑 scripts/build_replay_fixture.py"
        )
    conn = connect(":memory:")
    try:
        for table in _TABLES:
            parquet = (fixture_dir / f"{table}.parquet").as_posix()
            # SCHEMA_SQL 已预建空表，必须 OR REPLACE 覆盖
            conn.execute(
                f"CREATE OR REPLACE TABLE {table} AS SELECT * FROM read_parquet('{parquet}')"
            )
        apply_suspension_seeds(conn)
        return Snapshot.from_duckdb(conn)
    finally:
        conn.close()


def run_replay(fixture_dir: Path = FIXTURE_DIR) -> tuple[Snapshot, object, list[ReplayCaseResult]]:
    """回放巡检并逐 case 断言。返回 (快照, InspectionResult, case 结果列表)。"""
    golden = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    asof = date.fromisoformat(str(golden["asof"]))

    snap = load_replay_snapshot(fixture_dir).as_of(asof)
    result = run_inspection(snap)

    # (probe, symbol, window) → 判定信息。告警会被合并（同表同标的同根因），
    # 非头部窗口共用头部 diagnosis——合并的前提就是根因相同
    index: dict[tuple[str, str, str], dict] = {}
    for f, d in result.suppressed:
        index[(f.probe, f.symbol, f.window)] = {"finding": f, "diagnosis": d, "suppressed": True}
    for a in result.alerts:
        f = a.finding
        for w in [f.window, *a.extra_windows]:
            index[(f.probe, f.symbol, w)] = {
                "finding": f,
                "diagnosis": a.diagnosis,
                "suppressed": False,
            }

    cases: list[ReplayCaseResult] = []
    for case in golden["cases"]:
        name = str(case["name"])
        key = (
            str(case["finding"]["probe"]),
            str(case["finding"]["symbol"]),
            str(case["finding"]["window"]),
        )
        expect = case["expect"]
        hit = index.get(key)
        if hit is None:
            cases.append(
                ReplayCaseResult(name, False, f"回放未产出该信号 {key}（探针漏检或 fixture 漂移）")
            )
            continue
        d = hit["diagnosis"]
        problems = []
        if hit["suppressed"] != bool(expect["suppressed"]):
            problems.append(f"抑制期望不符：got suppressed={hit['suppressed']}")
        if d.root_cause.value != str(expect["root_cause"]):
            problems.append(f"根因不符：got {d.root_cause.value}")
        for ref in expect.get("evidence_refs", []):
            if not any(str(ref) in r for r in d.evidence_refs):
                problems.append(f"证据链缺 {ref}（有：{d.evidence_refs}）")
        cases.append(ReplayCaseResult(name, not problems, "; ".join(problems) or "通过"))

    for field, expected in (golden.get("expect_summary") or {}).items():
        got = getattr(result.summary, str(field), None)
        if got != expected:
            cases.append(
                ReplayCaseResult(
                    f"汇总 {field}", False, f"期望 {expected}，实际 {got}（巡检行为漂移）"
                )
            )
    return snap, result, cases


def run_strict() -> int:
    """门禁入口：任何 case 失败即非零退出。"""
    try:
        _snap, result, cases = run_replay()
    except FileNotFoundError as exc:
        print(f"✗ {exc}", file=sys.stderr)
        return 2
    s = result.summary
    board = result.badges
    badge_stats = (
        f"已核验 {len(board.by_level(BadgeLevel.VERIFIED))}"
        f" 基线 {len(board.by_level(BadgeLevel.BASELINE))}"
        f" 仅借鉴 {len(board.by_level(BadgeLevel.CAUTION))}"
        f" 不可用 {len(board.by_level(BadgeLevel.UNUSABLE))}"
    )
    print(
        f"[replay] asof={s.asof} 信号={s.n_findings} 告警={s.n_alerts} "
        f"抑制={s.n_suppressed} 健康分={s.health_score}｜徽章：{badge_stats}"
    )
    failed = 0
    for c in cases:
        mark = "✓" if c.ok else "✗"
        print(f"  {mark} {c.name}: {c.detail}")
        failed += 0 if c.ok else 1
    total = len(cases)
    print(f"\n真实回放结果：{total - failed}/{total} 通过")
    return 1 if failed else 0
