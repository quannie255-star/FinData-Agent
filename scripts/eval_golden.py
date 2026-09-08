"""M5 评测门禁：跑 golden set，断言偏差。

用法：
    uv run python scripts/eval_golden.py            # 跑所有 case
    uv run python scripts/eval_golden.py --strict    # 任何 mismatch 即退出非 0

退出码：
    0   所有 case 通过
    1   有 case 失败
    2   数据仓库缺失
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from findata.core.db import connect
from findata.semantic.tools import metric_query

GOLDEN_PATH = Path(__file__).resolve().parent.parent / "eval" / "golden" / "golden.yaml"
FIXTURE_DB = Path(__file__).resolve().parent.parent / "data" / "eval_fixture.duckdb"


def _resolve_db(db_path: str | None) -> str:
    """决定评测用哪个数据仓库：显式 --db > 合成 fixture（golden 绑定 fixture）。

    golden set 的数值钉死在合成 fixture 上（确定性、不随时间漂移），
    因此默认就跑 fixture；真实仓库数值会随时间变化，不在 golden 范围内，
    需用 --db 显式指定并可预期失败。
    """
    if db_path:
        return db_path
    if not FIXTURE_DB.exists():
        from scripts.build_eval_fixture import build

        build(str(FIXTURE_DB))
    return str(FIXTURE_DB)


def run(strict: bool, db_path: str | None = None) -> int:
    target = _resolve_db(db_path)
    if not Path(target).exists():
        print(f"数据仓库不存在：{target}", file=sys.stderr)
        print("请先跑：uv run python scripts/ingest_finance.py", file=sys.stderr)
        print("或用 fixture：uv run python scripts/build_eval_fixture.py", file=sys.stderr)
        return 2

    cases = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))["cases"]
    conn = connect(target)
    passed, failed = 0, []
    try:
        for case in cases:
            name = case["name"]
            q = case["query"]
            expect = case["expect"]
            tol = float(case.get("tol", 1e-6))
            exp_val = expect.get("value")

            try:
                r = metric_query(conn, q["metric"], q.get("params", {}))
            except Exception as exc:
                failed.append((name, f"执行失败：{exc}"))
                continue

            # 数值断言（golden set 的核心：钉死数字）
            if expect["value_present"]:
                if r.value is None:
                    failed.append((name, "期望有值但返回 None"))
                    continue
                if exp_val is not None and abs(r.value - float(exp_val)) > tol:
                    failed.append(
                        (name, f"数值偏差超容差：{r.value} vs 期望 {exp_val}（tol={tol}）")
                    )
                    continue
            else:
                if r.value is not None:
                    failed.append((name, f"期望无值但返回 {r.value}"))
                    continue

            # 单位断言
            if "unit" in expect and r.unit != expect["unit"]:
                failed.append((name, f"单位不符：{r.unit} vs {expect['unit']}"))
                continue

            # 验证状态断言（核心）
            if "verification" in expect:
                got_status = r.verification.status.value if r.verification else "none"
                if got_status != expect["verification"]:
                    if strict or expect["verification"] == "verified":
                        failed.append(
                            (name, f"验证状态不符：{got_status} vs {expect['verification']}")
                        )
                        continue
                    else:
                        # 非 strict 模式下，期望 mismatch/not_verifiable 时允许 verified
                        want = expect["verification"]
                        print(f"  [warn] {name}: 验证状态 {got_status}（期望 {want}）")

            passed += 1
            print(f"  ✓ {name:24s} {r.label} = {r.value} {r.unit}")
    finally:
        conn.close()

    total = len(cases)
    print(f"\n评测结果：{passed}/{total} 通过")
    if failed:
        print("\n失败明细：")
        for n, msg in failed:
            print(f"  ✗ {n}: {msg}")
        return 1
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Findata Golden Set 评测")
    p.add_argument("--strict", action="store_true", help="任何 mismatch 即视为失败")
    p.add_argument("--db", default=None, help="指定数据仓库路径（默认用配置 dsn）")
    args = p.parse_args()
    return run(strict=args.strict, db_path=args.db)


if __name__ == "__main__":
    raise SystemExit(main())