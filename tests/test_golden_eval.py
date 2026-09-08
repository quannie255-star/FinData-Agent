"""M5 评测门禁的快速测试：校验 golden 结构合法、不依赖真实数据。

golden set 已重定位到 eval/golden/golden.yaml（避开与监控 eval/ 包的目录冲突）。
"""

from __future__ import annotations

from pathlib import Path

import yaml

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "eval" / "golden" / "golden.yaml"


def test_golden_file_exists():
    assert GOLDEN_PATH.exists()


def test_golden_structure():
    data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert "cases" in data
    assert len(data["cases"]) >= 3
    for case in data["cases"]:
        assert "name" in case
        assert "query" in case
        assert "metric" in case["query"]
        assert "expect" in case
        assert "verification" in case["expect"]


def test_golden_metrics_are_known():
    """golden 里的指标必须在 catalog 里存在。"""
    from findata.semantic.catalog import MetricCatalog

    data = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    catalog = MetricCatalog()
    for case in data["cases"]:
        metric = case["query"]["metric"]
        assert metric in catalog.names(), (
            f"case '{case['name']}' 引用未知指标 {metric}"
        )


def test_eval_returns_2_on_missing_explicit_db(tmp_path):
    """显式传入不存在的 --db 时，脚本应返回 2 并提示。"""
    from scripts.eval_golden import run

    missing = tmp_path / "missing.duckdb"
    rc = run(strict=False, db_path=str(missing))
    assert rc == 2


def test_eval_runs_on_fixture(tmp_path):
    """无显式 db 时，脚本自动用合成 fixture 跑通门禁（确定性）。"""
    from scripts.build_eval_fixture import build
    from scripts.eval_golden import run

    fx = tmp_path / "auto.duckdb"
    build(str(fx))
    rc = run(strict=True, db_path=str(fx))
    assert rc == 0
