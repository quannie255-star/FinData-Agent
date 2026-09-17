"""历史停牌种子：人工核实的公司事件，写入 corporate_event 供归因器消费。

为什么必须手工种子：akshare 的事件接口只有「除权除息历史」和「当日停牌
快照」，拿不到历史停牌的起止区间。2026-09-15 真实告警复盘
（docs/reviews/2026-09-15-attribution.md）证明：这张表为空时，停牌抑制
规则在生产上是死代码，4 条真实缺口全部被误报成漏采工单。

每条种子都带公告级来源链接（见复盘「证据来源」一节），证据链要求：
徽章的「✓ 已核验」必须能点开看到归因依据，种子是这条链的最底层事实。

幂等：按主键 (symbol, date, kind) upsert，重复执行结果一致。
"""

from __future__ import annotations

from datetime import date

import duckdb
import pandas as pd

from findata.core.db import upsert_dataframe

# 4 笔已核实停牌（2026-09-15 复盘逐条核对过缺口窗口与公告日期）。
# end_date 必须覆盖缺口窗口的最后一个交易日，否则 _halted_days 的
# 覆盖率不足 0.9，抑制不生效。
SUSPENSION_SEEDS: list[dict] = [
    {
        "symbol": "600030",
        "date": date(2022, 1, 19),
        "kind": "suspension",
        "end_date": date(2022, 1, 26),
        "detail": (
            "中信证券 280 亿 A 股配股缴款期停牌（1/19-25）+ 网上清算（1/26），1/27 复牌。"
            "来源: https://www.cs.ecitic.com/newsite/tzzgx/ggyth/gg/aggg/202201/P020220123582042574716.pdf"
            " ; https://www.cls.cn/detail/914865"
        ),
    },
    {
        "symbol": "600900",
        "date": date(2022, 10, 26),
        "kind": "suspension",
        "end_date": date(2022, 10, 26),
        "detail": (
            "长江电力：证监会并购重组委当日审核乌东德/白鹤滩资产注入方案，全天停牌。"
            "来源: https://vip.stock.finance.sina.com.cn/corp/view/vCB_AllBulletinDetail.php?stockid=600900&id=8611712"
            " ; https://www.cindasc.com/osoa/views/a/20221026/46443.html"
        ),
    },
    {
        "symbol": "601088",
        "date": date(2025, 8, 4),
        "kind": "suspension",
        "end_date": date(2025, 8, 15),
        "detail": (
            "中国神华：1336 亿重大资产重组（收购国家能源集团旗下资产）停牌约两周，8/18 复牌涨停。"
            "来源: https://www.stdaily.com/web/gdxw/2026-03/21/content_489818.html"
        ),
    },
    {
        "symbol": "688981",
        "date": date(2025, 9, 1),
        "kind": "suspension",
        "end_date": date(2025, 9, 8),
        "detail": (
            "中芯国际：筹划发行股份购买中芯北方 49% 股权，8/29 公告、9/1 起停牌。"
            "来源: https://www.jfdaily.com/news/detail?id=974512"
            " ; https://www.yicai.com/news/102800879.html"
        ),
    },
]


def seeds_frame() -> pd.DataFrame:
    return pd.DataFrame(SUSPENSION_SEEDS)


def apply_suspension_seeds(conn: duckdb.DuckDBPyConnection) -> int:
    """把停牌种子幂等写入 corporate_event，返回本次新增的种子条数（重跑为 0）。

    upsert 先删同键旧行再插入，内容不变；返回值只统计真正新落库的种子，
    让调用方（日报知识层状态、采集日志）能区分「首次写入」和「例行检查」。
    """
    have = {
        (str(s), d if isinstance(d, date) else d.date())
        for s, d in conn.execute(
            "SELECT symbol, date FROM corporate_event WHERE kind = 'suspension'"
        ).fetchall()
    }
    pending = [s for s in SUSPENSION_SEEDS if (s["symbol"], s["date"]) not in have]
    if not pending:
        return 0
    upsert_dataframe(conn, "corporate_event", seeds_frame(), ["symbol", "date", "kind"])
    return len(pending)
