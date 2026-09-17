"""通用包（v2.2.0）测试：探针、归因、徽章、诚实边界、IO 与 CLI。

两条铁律级断言：
1. 通用包永远给不出 ✓ 已核验（无领域知识是它的定义性约束，不是缺陷）；
2. P0 故障在混排信号下必须压过 P1/P2（⚠️ 不能掩盖 ✗）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path  # noqa: E402

import duckdb
import numpy as np
import pandas as pd
import pytest

from findata.cli.trust_report import main as cli_main
from findata.dq.badges import BadgeLevel
from findata.dq.models import Finding, RootCause, Severity
from findata.generic.engine import render_markdown, run_generic_inspection
from findata.generic.io import load_table
from findata.generic.probes import GenericContext, run_probes
from findata.generic.schema import ColumnSpec, TableSpec, from_yaml, infer
from findata.generic.triage import _ROUTE_METRICS, GenericTriage

REPO = Path(__file__).resolve().parent.parent
AIR = REPO / "eval" / "fixtures" / "generic" / "air_quality"
ECOM = REPO / "eval" / "fixtures" / "generic" / "ecommerce"


def _clean_df(n: int = 120) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    ts = pd.date_range("2026-06-01", periods=n, freq="h")
    return pd.DataFrame(
        {
            "device_id": [f"D{i:04d}" for i in range(n)],
            "region": rng.choice(["east", "west", "north"], n),
            "temperature": rng.normal(25, 2, n).round(2),
            "humidity": rng.normal(60, 5, n).round(2),
            "recorded_at": ts,
        }
    )


def _clean_spec() -> TableSpec:
    return TableSpec(
        table="devices",
        columns=(
            ColumnSpec("device_id", "id", nullable=False),
            ColumnSpec("region", "categorical", nullable=False),
            ColumnSpec("temperature", "numeric"),
            ColumnSpec("humidity", "numeric"),
            ColumnSpec("recorded_at", "datetime", nullable=False),
        ),
        primary_key=("device_id",),
        timestamp_column="recorded_at",
        freshness_days=7,
        asof=date(2026, 6, 6),
    )


def _dirty_df() -> pd.DataFrame:
    df = _clean_df()
    df.loc[3:9, "temperature"] = np.nan  # 空值超容忍
    df.loc[15, "humidity"] = 99999.0  # 极端离群
    df = pd.concat([df, df.iloc[[0, 1]]], ignore_index=True)  # 主键重复
    # 类型漂移：数值声明列里混进不可解析字符串（先转 object 才能写入）。
    # 探针阈值是 1%，注入 2/120 才能越过
    df["temperature"] = df["temperature"].astype(object)
    df.loc[df.index[-1], "temperature"] = "N/A"
    df.loc[df.index[-2], "temperature"] = "error"
    return df


# ─────────────────── 探针 ───────────────────


def test_clean_data_is_all_baseline():
    r = run_generic_inspection(_clean_df(), _clean_spec(), declared=True)
    assert r.findings == []
    assert all(b.level is BadgeLevel.BASELINE for b in r.board.badges)
    assert r.board.health_score == 100.0
    assert r.skipped == []


def test_each_probe_fires_on_its_fault():
    ctx = GenericContext(df=_dirty_df(), spec=_clean_spec(), asof=date(2026, 6, 6))
    findings = run_probes(ctx)
    metrics = {f.metric.split("::")[0] for f in findings}
    assert "null_rate" in metrics  # 完整性
    assert "duplicate_keys" in metrics  # 唯一性
    assert "outlier_rate" in metrics or "level_shift" in metrics  # 离群值
    assert any(f.metric.startswith("type_mismatch") for f in findings)  # Schema 一致
    # 新鲜度：脏数据时间戳没动，不应误报
    assert not any(f.probe == "freshness" for f in findings)


def test_staleness_fires():
    spec = _clean_spec()
    df = _clean_df()
    r = run_generic_inspection(
        df, spec, declared=True, asof=date(2026, 7, 1)
    )
    assert any(f.metric == "stale_days" for f in r.findings)


# ─────────────────── 归因与诚实边界 ───────────────────


def test_generic_never_verifies():
    """铁律：通用归因器永远不产出 BENIGN_*/抑制，徽章里永远没有 ✓ 已核验。"""
    ctx = GenericContext(df=_dirty_df(), spec=_clean_spec(), asof=date(2026, 6, 6))
    findings = run_probes(ctx)
    diagnoses = GenericTriage().diagnose_all(findings, ctx)
    assert all(not d.root_cause.is_benign for d in diagnoses)
    assert all(not d.suppressed for d in diagnoses)
    r = run_generic_inspection(_dirty_df(), _clean_spec(), declared=True)
    assert r.board.by_level(BadgeLevel.VERIFIED) == []
    # 全路由遍历：任何根因都到不了 benign 分支
    for metric in _ROUTE_METRICS:
        f = Finding(
            probe="x", table="t", symbol="c", window="全表",
            metric=metric, value=1, threshold=0, severity_hint=Severity.P1,
        )
        d = GenericTriage().diagnose(f, ctx)
        assert not d.root_cause.is_benign and not d.suppressed


def test_unknown_signal_is_conservative():
    ctx = GenericContext(df=_clean_df(), spec=_clean_spec(), asof=date(2026, 6, 6))
    f = Finding(
        probe="mystery", table="t", symbol="c", window="全表",
        metric="warp_drive_failure", value=42, threshold=0,
        severity_hint=Severity.P1,
    )
    d = GenericTriage().diagnose(f, ctx)
    assert d.root_cause is RootCause.UNKNOWN
    assert d.severity is Severity.P1  # 保守保留原级别，不静默丢弃


def test_p0_dominates_mixed_findings():
    """回归：P0（主键重复）与 P1/P2 混排时，✗ 必须压过 ⚠️。"""
    df = _dirty_df()  # 既有主键重复（P0）又有空值/离群（P1/P2）
    r = run_generic_inspection(df, _clean_spec(), declared=True)
    for b in r.board.badges:
        if b.column in ("device_id", "region", "recorded_at"):
            assert b.level is BadgeLevel.UNUSABLE, f"{b.column} 应被 P0 打成 ✗"


# ─────────────────── schema 与 IO ───────────────────


def test_inferred_schema_degrades_visibly():
    r = run_generic_inspection(_clean_df(), infer("devices", _clean_df()), declared=False)
    assert not r.declared
    assert any("唯一性" in s for s in r.skipped)
    assert any("Schema" in s for s in r.skipped)
    md = render_markdown(r)
    assert "自动推断" in md and "降级" in md


def test_schema_yaml_roundtrip(tmp_path):
    spec = from_yaml(ECOM / "schema.yaml")
    assert spec.primary_key == ("order_id",)
    assert spec.timestamp_column == "order_ts"
    assert spec.group_column == "category"
    # 非法 kind 必须显式拒绝
    bad = tmp_path / "bad.yaml"
    lines = ["table: t", "columns:", "  a: {kind: telepathic}"]
    bad.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="kind"):
        from_yaml(bad)


def test_io_csv_duckdb_sqlite(tmp_path):
    df = _clean_df().head(20)
    csv = tmp_path / "t.csv"
    df.to_csv(csv, index=False)
    loaded, desc = load_table(csv)
    assert len(loaded) == 20 and desc.startswith("csv")

    con = duckdb.connect(str(tmp_path / "w.duckdb"))
    con.register("df20", df)
    con.execute("CREATE TABLE t20 AS SELECT * FROM df20")
    con.close()
    loaded, desc = load_table(tmp_path / "w.duckdb", table="t20")
    assert len(loaded) == 20 and desc.startswith("duckdb")

    import sqlite3

    con = sqlite3.connect(str(tmp_path / "s.db"))
    df.to_sql("t20", con, index=False)
    con.close()
    loaded, desc = load_table(tmp_path / "s.db", table="t20")
    assert len(loaded) == 20 and desc.startswith("sqlite")

    with pytest.raises(ValueError):
        load_table(tmp_path / "s.db")  # 库类不给表名必须报错


# ─────────────────── 公开数据集端到端 + CLI ───────────────────


def test_public_datasets_end_to_end():
    """验收①：两个公开/演示数据集端到端出带徽章报告。"""
    # 真实公开数据（UCI Beijing PM2.5，2013 切片）：陈旧数据必须被新鲜度拦下
    df, _ = load_table(AIR / "beijing_pm25.csv")
    spec = from_yaml(AIR / "schema.yaml")
    r = run_generic_inspection(df, spec, source="csv", declared=True)
    assert any(f.metric == "stale_days" for f in r.findings)
    assert all(b.level is BadgeLevel.CAUTION for b in r.board.badges)
    # 合成演示数据（脱敏电商）：注入的主键重复必须打成 ✗
    df2, _ = load_table(ECOM / "orders.csv")
    spec2 = from_yaml(ECOM / "schema.yaml")
    r2 = run_generic_inspection(df2, spec2, source="csv", declared=True)
    assert any(f.metric == "duplicate_keys" for f in r2.findings)
    assert r2.board.by_level(BadgeLevel.UNUSABLE)
    assert r2.board.by_level(BadgeLevel.VERIFIED) == []


def test_cli_trust_report(tmp_path):
    out_md = tmp_path / "r.md"
    out_html = tmp_path / "r.html"
    code = cli_main(
        [str(ECOM / "orders.csv"), "--schema", str(ECOM / "schema.yaml"),
         "-o", str(out_md), "--html", str(out_html)]
    )
    assert code == 0
    assert out_md.exists() and out_html.exists()
    assert "通用数据可信报告" in out_md.read_text(encoding="utf-8")
    # --strict：注入故障的表有 ✗，必须退出 1
    code2 = cli_main(
        [str(ECOM / "orders.csv"), "--schema", str(ECOM / "schema.yaml"), "--strict"]
    )
    assert code2 == 1
    code3 = cli_main([str(tmp_path / "missing.csv")])
    assert code3 == 2
