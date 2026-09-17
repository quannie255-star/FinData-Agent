"""构建通用包的公开数据集 demo fixtures（R3 验收）。

用法（需网络，只在构建时跑一次；产物入库，测试/演示离线复用）：
    uv run python scripts/build_generic_fixtures.py

产出 eval/fixtures/generic/ 下两个数据集：
1. air_quality/beijing_pm25 —— UCI "Beijing PM2.5 Data"（真实公开数据，
   含缺失与重尾，2013 年切片）；来源: UCI ML Repository, CC BY 4.0
2. ecommerce/orders —— 脱敏电商订单（确定性合成，结构对标公开电商
   订单数据；注入了少量重复主键与近期金额水平迁移，供报告演示）

demo 数据的诚实原则：真实数据标注真实来源与口径；合成数据明确标注
"合成"，绝不冒充公开数据集。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "eval" / "fixtures" / "generic"

# UCI 2024 改版后的静态分发路径（zip 内含 PRSA_data_2010.2014.1.19.csv）
UCI_PM25_URL = "https://archive.ics.uci.edu/static/public/381/beijing+pm2+5+data.zip"


def build_air_quality() -> Path:
    """UCI 北京 PM2.5：2013-01..03 切片（整点，单站点）。真实数据，勿伪造。"""
    print(f"[air] 下载 UCI Beijing PM2.5：{UCI_PM25_URL}")
    import io
    import zipfile

    resp = requests.get(UCI_PM25_URL, timeout=120)
    resp.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".csv"))
        full = pd.read_csv(io.BytesIO(zf.read(name)))
    sub = full[(full["year"] == 2013) & (full["month"] <= 3)].copy()
    out = pd.DataFrame(
        {
            "datetime": pd.to_datetime(
                dict(year=sub["year"], month=sub["month"], day=sub["day"], hour=sub["hour"])
            ),
            "pm25": sub["pm2.5"],
            "temp": sub["TEMP"],
            "dewp": sub["DEWP"],
            "pres": sub["PRES"],
            "iws": sub["Iws"].round(2),
            "cbwd": sub["cbwd"],
        }
    )
    d = OUT / "air_quality"
    d.mkdir(parents=True, exist_ok=True)
    csv = d / "beijing_pm25.csv"
    out.to_csv(csv, index=False)
    (d / "schema.yaml").write_text(
        """# UCI Beijing PM2.5 Data（CC BY 4.0），2013-01..03 单站点整点切片
table: beijing_pm25
timestamp_column: datetime
primary_key: datetime
freshness_days: 30
columns:
  datetime: {kind: datetime, nullable: false, description: 观测时间（北京时间，整点）}
  pm25: {kind: numeric, nullable: true, null_tolerance: 0.05, description: PM2.5 浓度}
  temp: {kind: numeric, description: 气温（摄氏度）}
  dewp: {kind: numeric, description: 露点}
  pres: {kind: numeric, description: 气压（hPa）}
  iws: {kind: numeric, description: 累积风速（m/s）}
  cbwd: {kind: categorical, description: 风向}
""",
        encoding="utf-8",
    )
    print(f"[air] 写出 {csv.relative_to(ROOT)}（{len(out)} 行）")
    return csv


def build_ecommerce() -> Path:
    """脱敏电商订单：确定性合成（seed 固定），注入两类演示问题。"""
    rng = np.random.default_rng(20260917)
    n = 5000
    start = date.today() - timedelta(days=90)
    ts = pd.to_datetime(start) + pd.to_timedelta(rng.integers(0, 90 * 24 * 60, n), unit="m")
    categories = np.array(["家电", "服饰", "食品", "图书", "数码"])
    cat = rng.choice(categories, n, p=[0.22, 0.28, 0.2, 0.12, 0.18])
    base_price = {"家电": 800, "服饰": 180, "食品": 45, "图书": 55, "数码": 1200}
    amounts = np.array([base_price[c] * float(rng.lognormal(0, 0.35)) for c in cat])
    # 演示问题①：最近 20% 时间的"数码"类金额整体 ×3（水平迁移，疑点）
    cutoff = np.quantile(ts.values, 0.8)
    recent = ts.values >= cutoff
    # ×4 而不是 ×3：漂移阈值在 3.0，注入倍率必须明确越过边界，
    # 否则浮点均值比 2.999x 会让 level_shift 静默漏检（实测踩过）
    amounts = np.where(recent & (cat == "数码"), amounts * 4.0, amounts)
    qty = rng.integers(1, 4, n)
    df = pd.DataFrame(
        {
            "order_id": [f"ORD{i:06d}" for i in range(n)],
            "user_id": [f"U{v:05d}" for v in rng.integers(1, 1200, n)],
            "category": cat,
            "amount": amounts.round(2),
            "quantity": qty,
            "order_ts": pd.Series(ts).dt.floor("s"),
        }
    )
    # 演示问题②：5 个主键各重复一次（幂等缺失，✗ 不可用）
    dup_idx = rng.choice(n, 5, replace=False)
    dups = df.iloc[dup_idx].copy()
    df = pd.concat([df, dups], ignore_index=True)

    d = OUT / "ecommerce"
    d.mkdir(parents=True, exist_ok=True)
    csv = d / "orders.csv"
    df.sort_values("order_ts").to_csv(csv, index=False)
    (d / "schema.yaml").write_text(
        """# 脱敏电商订单（确定性合成演示数据，结构对标公开电商订单集；非真实交易）
table: orders
timestamp_column: order_ts
primary_key: order_id
freshness_days: 90
outlier_tolerance: 0.05
group_column: category
columns:
  order_id: {kind: id, nullable: false, description: 订单号（主键）}
  user_id: {kind: id, nullable: false, description: 脱敏用户号}
  category: {kind: categorical, nullable: false, description: 商品类目}
  amount: {kind: numeric, nullable: false, null_tolerance: 0.0, description: 订单金额（元）}
  quantity: {kind: numeric, nullable: false, description: 件数}
  order_ts: {kind: datetime, nullable: false, description: 下单时间}
""",
        encoding="utf-8",
    )
    print(f"[ecom] 写出 {csv.relative_to(ROOT)}（{len(df)} 行，seed=20260917 可复现）")
    return csv


def main() -> int:
    build_air_quality()
    build_ecommerce()
    print(f"fixtures 构建完成：{OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
