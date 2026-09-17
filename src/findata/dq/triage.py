"""规则归因器（baseline）：把 Finding 判成 Diagnosis。

它存在的三个理由，缺一不可：

1. **零成本可跑**：不依赖 LLM，离线、确定性、CI 里能当门禁
2. **作为对比基线**：后面接 LLM 归因器时，用同一套评测集证明增量价值，
   而不是拍脑袋说"加了大模型效果更好"——那句话面试官听过一百遍
3. **把领域知识显式化**：规则写清楚之后，LLM 到底该学什么也就清楚了。
   规则覆盖不到的部分（模糊归因、跨源推理）才是 LLM 真正的用武之地

核心思路：同一个"数据缺失"信号，结合不同证据会指向完全不同的结论——
- 窗口落在停牌区间 → 合法，抑制
- 窗口内本就没有交易日 → 合法，抑制
- 数据完整但上市晚 → 新股，抑制
- 采集血缘里有 failed run → 上游故障，P0
- 尾部停更 → 停更；中间空洞 → 漏采
"""

from __future__ import annotations

import json
from datetime import date

import pandas as pd

from findata.dq.models import Diagnosis, Finding, RootCause, Severity
from findata.dq.probes import _MIN_HISTORY_DAYS, ProbeContext

# 窗口内被合法事件覆盖达到这个比例，即判定为可抑制
_BENIGN_COVERAGE = 0.9
# 上市后数据完整率达到这个比例，才敢认定"历史短"是上市晚导致而非数据缺失
_COMPLETE_RATIO = 0.9

# ── 截面共动判据（R1.2，P0b）。阈值不是拍的：2026-09-15 复盘对真实窗口
# 实测标定——924 行情 24-25/25 标的 ≥2x、沪深300 指数 2.2-2.8x；724 行情
# 全市场仅 6/25 ≥2x（指数 ≤1.45x，全市场判据不命中）但大金融板块 3 只同伴
# ≥2x；2023-03 中芯国际为孤立事件（同伴 ≤1.6x、指数 1.22x），必须保留告警。
_MARKET_SHARE = 0.5  # 同窗 ≥ 半数可比标的放量 ≥2x → 全市场行情
_PEER_X = 2.0  # 同伴放量判定线
_INDEX_X = 1.5  # 指数量能比判定线
_SECTOR_PEER_MIN = 2  # 板块共动：≥2 只同板块同伴 ≥2x（不含自己）
_RATIO_HISTORY_DAYS = 30  # 共动倍率基线：窗口前 30 个交易日均量（与漂移探针同口径）

# 行业 → 板块映射（A 股领域包知识）。截面共动看的是板块而不是细分行业：
# 724 行情里券商/银行/保险/金融科技同涨，而股票池里 600030 是唯一券商，
# 只按 industry 同名列分组会让板块共动永远无法命中。未登记的行业映射到
# 自身（单成员板块，板块判据自然失效）——宁可漏抑制，不可错抑制。
_SECTOR_OF_INDUSTRY: dict[str, str] = {
    "券商": "大金融",
    "银行": "大金融",
    "保险": "大金融",
    "金融科技": "大金融",
    "半导体": "半导体链",
    "半导体设备": "半导体链",
    "消费电子": "电子链",
    "通信": "电子链",
    "安防": "电子链",
    "白酒": "食品饮料",
    "食品饮料": "食品饮料",
    "创新药": "医药医疗",
    "医疗器械": "医药医疗",
    "新能源汽车": "新能源链",
    "动力电池": "新能源链",
    "光伏": "新能源链",
    "电力": "能源资源",
    "煤炭": "能源资源",
    "有色金属": "能源资源",
    "化工": "化工",
    "家电": "家电",
}


def _parse_window(window: str) -> tuple[date, date] | None:
    if ".." not in window:
        return None
    head, tail = window.split("..", 1)
    try:
        return date.fromisoformat(head), date.fromisoformat(tail)
    except ValueError:
        return None


def _list_date(ctx: ProbeContext, symbol: str) -> date | None:
    if ctx.universe.empty or "list_date" not in ctx.universe.columns:
        return None
    sub = ctx.universe.loc[ctx.universe["symbol"] == symbol, "list_date"]
    if len(sub) and pd.notna(sub.iloc[0]):
        return sub.iloc[0]
    return None


def _halted_days(ctx: ProbeContext, symbol: str) -> set[date]:
    """该股票处于停牌状态的全部交易日。"""
    if ctx.corporate_event.empty or "kind" not in ctx.corporate_event.columns:
        return set()
    sub = ctx.corporate_event[
        (ctx.corporate_event["symbol"] == symbol) & (ctx.corporate_event["kind"] == "suspension")
    ]
    out: set[date] = set()
    for row in sub.itertuples(index=False):
        end = row.end_date if pd.notna(row.end_date) else row.date
        out |= set(ctx.calendar.trading_days(row.date, end))
    return out


def _coverage(ctx: ProbeContext, start: date, end: date, day_set: set[date]) -> float:
    days = ctx.calendar.trading_days(start, end)
    if not days:
        return 0.0
    return sum(1 for d in days if d in day_set) / len(days)


def _has_event(ctx: ProbeContext, symbol: str, kind: str, start: date, end: date) -> bool:
    if ctx.corporate_event.empty or "kind" not in ctx.corporate_event.columns:
        return False
    # 日期列可能是 datetime64[us]（duckdb → pandas 的真实仓库路径），也可能是
    # object dtype 的 datetime.date（合成语料路径）。统一转 Timestamp 再比，
    # 否则 datetime.date 与 datetime64[us] 比较会抛 InvalidComparison。
    start_ts, end_ts = pd.Timestamp(start), pd.Timestamp(end)
    dates = pd.to_datetime(ctx.corporate_event["date"])
    sub = ctx.corporate_event[
        (ctx.corporate_event["symbol"] == symbol)
        & (ctx.corporate_event["kind"] == kind)
        & (dates >= start_ts)
        & (dates <= end_ts)
    ]
    return not sub.empty


def _failed_run(ctx: ProbeContext, symbol: str, table: str) -> str | None:
    """血缘表里是否存在失败采集。这是"数据缺失"能归因到上游的直接证据。"""
    if ctx.ingest_run.empty or "status" not in ctx.ingest_run.columns:
        return None
    sub = ctx.ingest_run[
        (ctx.ingest_run["target_table"] == table) & (ctx.ingest_run["status"] == "failed")
    ]
    for row in sub.itertuples(index=False):
        raw = getattr(row, "params", None)
        try:
            params = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except ValueError:
            params = {}
        if params.get("symbol") in (None, symbol):
            return str(row.run_id)
    return None


def _sector_of(ctx: ProbeContext, symbol: str) -> str:
    if ctx.universe.empty or "industry" not in ctx.universe.columns:
        return ""
    sub = ctx.universe.loc[ctx.universe["symbol"] == symbol, "industry"]
    if not len(sub) or pd.isna(sub.iloc[0]):
        return ""
    industry = str(sub.iloc[0])
    return _SECTOR_OF_INDUSTRY.get(industry, industry)


def _window_volume_ratio(
    dates: pd.Series, volume: pd.Series, start: date, end: date
) -> float | None:
    """单标的在 [start, end] 窗口的量能比：窗口均量 / 窗口前 30 个交易日均量。

    与漂移探针同口径（10 日窗口均值对 30 日基线），倍率才有可比性。
    窗口前历史不足 30 日的标的不可比，返回 None 从分母中剔除。
    """
    d = pd.to_datetime(dates).dt.date
    idx = [i for i, dd in enumerate(d) if start <= dd <= end]
    if not idx:
        return None
    lo, hi = idx[0], idx[-1]
    if lo < _RATIO_HISTORY_DAYS:
        return None
    vol = pd.to_numeric(volume, errors="coerce")
    recent = float(vol.iloc[lo : hi + 1].mean())
    hist = float(vol.iloc[lo - _RATIO_HISTORY_DAYS : lo].mean())
    if hist <= 0 or pd.isna(recent) or pd.isna(hist):
        return None
    return recent / hist


def _co_movement(ctx: ProbeContext, symbol: str, start: date, end: date) -> dict | None:
    """同窗截面共动证据：全市场放量分布 + 指数量能 + 同板块同伴。

    这是漂移归因缺的那类"仓库里就有的事实"（复盘根因二）：量价异常
    是不是行情，横向问一遍同期所有标的就知道。返回 None 表示无截面证据
    可言（无可比标的），此时漂移维持原判。
    """
    stats: list[tuple[str, float, str]] = []
    self_sector = _sector_of(ctx, symbol)
    # 最小上下文替身（如 LLM 归因单测的 fake ctx）可能给无列空表：
    # 没有截面可言，直接放弃共动证据，漂移维持原判
    if ctx.stock_daily.empty or "symbol" not in ctx.stock_daily.columns:
        return None
    for sym, g in ctx.stock_daily.groupby("symbol"):
        ratio = _window_volume_ratio(g["date"], g["volume"], start, end)
        if ratio is not None:
            stats.append((str(sym), ratio, _sector_of(ctx, str(sym))))
    if not stats:
        return None

    n = len(stats)
    n_ge2 = sum(1 for _, r, _ in stats if r >= _PEER_X)
    index_ratios: dict[str, float] = {}
    if not ctx.index_daily.empty:
        for ix, g in ctx.index_daily.groupby("symbol"):
            r = _window_volume_ratio(g["date"], g["volume"], start, end)
            if r is not None:
                index_ratios[str(ix)] = r
    sector_peers = sorted(
        ((s, r) for s, r, sec in stats if s != symbol and sec == self_sector and r >= _PEER_X),
        key=lambda x: -x[1],
    )
    top = sorted(stats, key=lambda x: -x[1])[:5]
    return {
        "n": n,
        "n_ge2": n_ge2,
        "index_ratios": index_ratios,
        "sector": self_sector,
        "sector_peers": sector_peers,
        "top": top,
        "market_hit": (n_ge2 / n) >= _MARKET_SHARE
        or any(r >= _INDEX_X for r in index_ratios.values()),
        "sector_hit": len(sector_peers) >= _SECTOR_PEER_MIN,
    }


class BaselineTriage:
    """规则归因器。diagnose 是纯函数，便于单测与替换。"""

    def diagnose(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        route = {
            "freshness": self._diagnose_gap,
            "completeness": self._diagnose_gap,
            "history_depth": self._diagnose_depth,
            "validity": self._diagnose_validity,
            "uniqueness": self._diagnose_uniqueness,
            "drift": self._diagnose_drift,
            "null_sweep": self._diagnose_null,
        }.get(finding.probe)
        if route is None:
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.UNKNOWN,
                severity=finding.severity_hint,
                confidence=0.3,
                explanation=f"探针 {finding.probe} 无匹配规则，保守保留原级别",
            )
        return route(finding, ctx)

    def diagnose_all(self, findings: list[Finding], ctx: ProbeContext) -> list[Diagnosis]:
        return [self.diagnose(f, ctx) for f in findings]

    # ---- 各类信号的归因规则 ----

    def _diagnose_gap(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        parsed = _parse_window(finding.window)
        if parsed is None:
            return self._unknown(finding, "窗口格式无法解析")

        start, end = parsed
        halted = _halted_days(ctx, finding.symbol)
        cov = _coverage(ctx, start, end, halted)
        if cov >= _BENIGN_COVERAGE:
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.BENIGN_SUSPENSION,
                severity=Severity.OK,
                confidence=0.95,
                explanation=(
                    f"窗口内 {cov:.0%} 交易日处于停牌区间，缺失为合法业务状态，告警已抑制"
                ),
                evidence_refs=(f"corporate_event:suspension:{finding.symbol}",),
            )

        if not ctx.calendar.trading_days(start, end):
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.BENIGN_NON_TRADING_DAY,
                severity=Severity.OK,
                confidence=0.95,
                explanation="窗口内不含交易日，本就不应有数据，告警已抑制",
            )

        run_id = _failed_run(ctx, finding.symbol, finding.table)
        if run_id:
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.UPSTREAM_OUTAGE,
                severity=Severity.P0,
                confidence=0.9,
                explanation=f"血缘表记录采集任务 {run_id} 失败，数据缺失由上游故障导致",
                evidence_refs=(f"ingest_run:{run_id}",),
            )

        if finding.probe == "freshness":
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.UPSTREAM_STALE,
                severity=Severity.P0 if finding.value >= 2 else Severity.P1,
                confidence=0.8,
                explanation=(
                    f"最新数据落后 {int(finding.value)} 个交易日，且无停牌/新股可解释，"
                    "判定为上游停更"
                ),
            )

        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.MISSING_ROWS,
            severity=finding.severity_hint,
            confidence=0.75,
            explanation=f"中间出现 {int(finding.value)} 个交易日空洞，判定为回补漏采",
        )

    def _diagnose_depth(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        n = int(finding.value)
        ld = _list_date(ctx, finding.symbol)
        if ld is not None:
            expected = ctx.calendar.trading_days(ld, ctx.asof)
            if expected:
                ratio = n / len(expected)
                if ratio >= _COMPLETE_RATIO and len(expected) <= _MIN_HISTORY_DAYS:
                    return Diagnosis(
                        finding_key=finding.key,
                        root_cause=RootCause.BENIGN_NEW_LISTING,
                        severity=Severity.OK,
                        confidence=0.9,
                        explanation=(
                            f"上市日 {ld}，应到 {len(expected)} 个交易日、实到 {n} 个，"
                            "数据完整，历史短属正常，告警已抑制"
                        ),
                        evidence_refs=(f"stock_universe:list_date:{finding.symbol}",),
                    )
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.MISSING_ROWS,
            severity=Severity.P1,
            confidence=0.6,
            explanation=f"历史仅 {n} 个交易日且无法用上市日解释，怀疑数据被删",
        )

    def _diagnose_validity(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        parsed = _parse_window(finding.window)
        if (
            finding.metric == "pct_chg_out_of_limit"
            and parsed is not None
            and _has_event(ctx, finding.symbol, "ex_rights", parsed[0], parsed[1])
        ):
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.BENIGN_CORPORATE_ACTION,
                severity=Severity.OK,
                confidence=0.9,
                explanation="当日存在除权除息事件，价格跳空为合法复权行为，告警已抑制",
                evidence_refs=(f"corporate_event:ex_rights:{finding.symbol}",),
            )
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.VALUE_OUT_OF_RANGE,
            severity=Severity.P0,
            confidence=0.85,
            explanation=f"{finding.metric} 命中 {int(finding.value)} 条，且无公司事件可解释",
        )

    def _diagnose_uniqueness(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.DUPLICATE_WRITE,
            severity=Severity.P0,
            confidence=0.95,
            explanation=f"{int(finding.value)} 个主键重复，任务重跑未做幂等",
        )

    def _diagnose_drift(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        if finding.metric == "total_mv_level_shift":
            return Diagnosis(
                finding_key=finding.key,
                root_cause=RootCause.UNIT_SHIFT,
                severity=Severity.P0,
                confidence=0.9,
                explanation=f"量级突变至 {finding.value:.6g} 倍，判定为单位或口径变更",
            )

        # 截面共动判据（R1.2 / P0b）：漂移归因先横向问一遍同期市场。
        # 上一版注释预言的"缺的不是算法，是事实来源"，事实就在仓库里——
        # 全市场放量分布、指数量能、板块同伴，都不需要外部数据。
        parsed = _parse_window(finding.window)
        if parsed is not None:
            co = _co_movement(ctx, finding.symbol, parsed[0], parsed[1])
            if co is not None and co["market_hit"]:
                ix = max(co["index_ratios"].values()) if co["index_ratios"] else None
                ix_txt = f"，指数最高 {ix:.2f}x" if ix is not None else ""
                return Diagnosis(
                    finding_key=finding.key,
                    root_cause=RootCause.BENIGN_MARKET_EVENT,
                    severity=Severity.OK,
                    confidence=0.9,
                    explanation=(
                        f"同窗全市场共动：{co['n_ge2']}/{co['n']} 标的放量 ≥{_PEER_X:.0f}x"
                        f"{ix_txt}——大盘级行情，非数据故障，告警已抑制"
                    ),
                    evidence_refs=(
                        "cross_section:market_volume",
                        *(f"index_daily:{k}" for k in sorted(co["index_ratios"])),
                    ),
                )
            if co is not None and co["sector_hit"]:
                peers = "、".join(f"{s}={r:.1f}x" for s, r in co["sector_peers"][:3])
                return Diagnosis(
                    finding_key=finding.key,
                    root_cause=RootCause.BENIGN_MARKET_EVENT,
                    severity=Severity.OK,
                    confidence=0.85,
                    explanation=(
                        f"同窗板块共动：{co['sector']}板块 ≥{_SECTOR_PEER_MIN} 只同伴放量"
                        f" ≥{_PEER_X:.0f}x（{peers}）——板块级行情，非数据故障，告警已抑制"
                    ),
                    evidence_refs=("cross_section:sector_volume",),
                )

        # 注意（已知局限）：音量放大无法在当前特征集下区分「单位/口径变更」与
        # 「市场放量」。探针算的是 10 日均量 / 前 30 日均量，会把注入的 20x
        # 稀释成 5.14x，而真实政策行情的放量是 5.40x —— 两者数值上几乎重合；
        # 价格判据同样无效（合成语料的价格本就是随机漫步，10 日涨跌 10% 很常见）。
        # 截面共动规则（上方）已把"全市场/板块级行情"分走；剩下的是
        # 2023-03 中芯国际型的**孤立事件**——个股级行情需要外部事实
        # （公告/新闻）才能确认，与停牌 / 上市日属于同一类问题：
        # 缺的不是算法，是事实来源。残留误报按观察级跟踪（复盘 P0b）。
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.DISTRIBUTION_DRIFT,
            severity=Severity.P1,
            confidence=0.8,
            explanation=f"近期均值/历史均值 = {finding.value:.2f}，超出漂移阈值",
        )

    def _diagnose_null(self, finding: Finding, ctx: ProbeContext) -> Diagnosis:
        col = finding.metric.split("::")[-1]
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.SCHEMA_NULL_SWEEP,
            severity=Severity.P1,
            confidence=0.85,
            explanation=f"列 {col} 近期空值率 {finding.value:.0%}，远高于历史，"
            "判定为上游字段下线",
        )

    def _unknown(self, finding: Finding, why: str) -> Diagnosis:
        return Diagnosis(
            finding_key=finding.key,
            root_cause=RootCause.UNKNOWN,
            severity=finding.severity_hint,
            confidence=0.3,
            explanation=why,
        )
