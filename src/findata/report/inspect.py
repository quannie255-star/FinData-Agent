"""每日巡检流水线：数据快照 → 探针 → 归因 → 告警路由 → 报告。

与评测 runner 的区别：runner 面向"系统准不准"，本模块面向"值班的人该做什么"。
所以它多做三件事：按严重度路由告警、给出仓库健康分、把结论渲染成人能读的报告。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from logging import getLogger

import pandas as pd

from findata.dq.calendar import TradingCalendar
from findata.dq.models import Diagnosis, Finding, RootCause, Severity
from findata.dq.probes import ProbeContext, run_probes
from findata.dq.triage import BaselineTriage
from findata.eval.fixtures import SyntheticConfig, build_baseline

# 告警路由：不同级别对应不同的响应时效与处理动作。
# 这是"产品"和"检测脚本"的分界线——只报不管的工具，最后都会被静音。
ROUTES: dict[Severity, tuple[str, str]] = {
    Severity.P0: ("立即响应", "推送值班群，阻断下游依赖该表的任务"),
    Severity.P1: ("当日工单", "进入当日待办，收盘前必须定位"),
    Severity.P2: ("周度汇总", "并入周报，Trend 跟踪即可"),
}

logger = getLogger(__name__)

_EMPTY_UNIVERSE = ("symbol", "name", "industry", "listed_board", "list_date")
# 列名必须与 db.SCHEMA_SQL 的 corporate_event 一致，否则降级空表会被
# 归因器当成"没有事件"而默默跳过抑制（列名不匹配时 triage 直接返回 False）。
_EMPTY_EVENT = ("symbol", "date", "kind", "end_date", "detail")
_EMPTY_RUN = ("run_id", "source", "table", "started_at", "status", "rows", "params")


def _calendar_from_warehouse(conn, observed: list) -> TradingCalendar:
    """真实仓库的交易日历：优先用 trading_calendar 表，没有才退回硬编码推断。

    为什么必须先查表：硬编码节假日只覆盖 2024-2025，而生产库常有更早历史。
    春节是**所有标的同一天休市**，数据里没有任何观测能反推出来（with_observed
    只认"出现过的日子"），所以唯一正解是接真实日历。拿真实数据巡检时漏了
    这一步，2022 年春节会被报成一串 missing_rows。
    """
    try:
        days = [
            d.date() if hasattr(d, "date") else d
            for (d,) in conn.execute("SELECT date FROM trading_calendar").fetchall()
        ]
    except Exception:
        days = []
    if not days:
        return TradingCalendar.default().with_observed(observed)
    cal = TradingCalendar.from_trading_days(days)
    logger.info("使用交易所真实交易日历：%d 个交易日", len(days))
    return cal.with_observed(observed)


def _empty(cols: tuple[str, ...]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in cols})


@dataclass
class Snapshot:
    """巡检输入：仓库在某一时点的一批表。"""

    stock_daily: pd.DataFrame
    valuation_daily: pd.DataFrame
    universe: pd.DataFrame
    corporate_event: pd.DataFrame
    ingest_run: pd.DataFrame
    calendar: TradingCalendar
    asof: date

    @classmethod
    def from_duckdb(cls, conn, asof: date | None = None) -> Snapshot:
        """从真实仓库装载。缺表时降级为空表而不是报错——巡检本身不该被元数据缺失打挂。"""

        def read(sql: str, cols: tuple[str, ...]) -> pd.DataFrame:
            try:
                return conn.execute(sql).df()
            except Exception:
                return _empty(cols)

        daily = read("SELECT * FROM stock_daily", ("symbol", "date"))
        if not daily.empty:
            daily["date"] = pd.to_datetime(daily["date"]).dt.date

        observed = sorted({d for d in daily.get("date", pd.Series(dtype=object)) if d is not None})
        cal = _calendar_from_warehouse(conn, observed)
        if asof is None:
            asof = observed[-1] if observed else date.today()

        return cls(
            stock_daily=daily,
            valuation_daily=read("SELECT * FROM valuation_daily", ("symbol", "date")),
            universe=read("SELECT * FROM stock_universe", _EMPTY_UNIVERSE),
            corporate_event=read("SELECT * FROM corporate_event", _EMPTY_EVENT),
            ingest_run=read("SELECT * FROM ingest_run", _EMPTY_RUN),
            calendar=cal,
            asof=asof,
        )

    @classmethod
    def from_synthetic(
        cls, cfg: SyntheticConfig | None = None, seed: int = 20240102
    ) -> Snapshot:
        """从确定性合成语料装载（含注入故障），用于零数据演示与回归。"""
        from findata.eval.faults import inject_faults

        base = build_baseline(cfg)
        injected = inject_faults(base, seed=seed)
        return cls(
            stock_daily=injected.stock_daily,
            valuation_daily=injected.valuation_daily,
            universe=base.universe,
            corporate_event=base.corporate_event,
            # 合成语料不带采集血缘，用空表；归因器会退化到无血缘证据的路径。
            ingest_run=getattr(base, "ingest_run", None) or _empty(_EMPTY_RUN),
            calendar=base.calendar,
            asof=injected.stock_daily["date"].max(),
        )

    def as_of(self, day: date) -> Snapshot:
        """回放：得到"站在 day 这天能看到的数据"。

        故障复盘必须能回到出事那天重跑巡检——用今天的全量数据去解释
        上个月的告警是没意义的，而且会引入未来数据导致结论失真。
        """
        daily = self._cut(self.stock_daily, day)
        return Snapshot(
            stock_daily=daily,
            valuation_daily=self._cut(self.valuation_daily, day),
            universe=self.universe,
            corporate_event=self._cut(self.corporate_event, day),
            ingest_run=self._cut(self.ingest_run, day, col="started_at"),
            calendar=self.calendar,
            asof=day,
        )

    @staticmethod
    def _cut(df: pd.DataFrame, day: date, col: str = "date") -> pd.DataFrame:
        if df.empty or col not in df.columns or not len(df[col]):
            return df
        return df[pd.to_datetime(df[col]).dt.date <= day]

    def to_probe_context(self) -> ProbeContext:
        return ProbeContext.of(
            stock_daily=self.stock_daily,
            valuation_daily=self.valuation_daily,
            universe=self.universe,
            corporate_event=self.corporate_event,
            ingest_run=self.ingest_run,
            calendar=self.calendar,
            asof=self.asof,
        )


@dataclass(frozen=True)
class Alert:
    """一条已定级、已路由、待处理的告警。"""

    finding: Finding
    diagnosis: Diagnosis
    route: str
    action: str
    merged: int = 1
    extra_windows: tuple[str, ...] = ()

    @property
    def windows(self) -> str:
        if not self.extra_windows:
            return self.finding.window
        return f"{self.finding.window} 等 {self.merged} 段"


@dataclass
class InspectionSummary:
    asof: date
    n_symbols: int = 0
    n_rows: int = 0
    n_findings: int = 0
    n_alerts: int = 0
    n_suppressed: int = 0
    by_severity: dict[str, int] = field(default_factory=dict)
    by_root_cause: dict[str, int] = field(default_factory=dict)
    health_score: float = 100.0
    noise_reduction: float = 0.0

    def grade(self) -> str:
        s = self.health_score
        if s >= 95:
            return "健康"
        if s >= 80:
            return "关注"
        if s >= 60:
            return "异常"
        return "严重"


@dataclass
class InspectionResult:
    asof: date
    alerts: list[Alert]
    suppressed: list[tuple[Finding, Diagnosis]]
    summary: InspectionSummary

    @property
    def p0(self) -> list[Alert]:
        return [a for a in self.alerts if a.diagnosis.severity is Severity.P0]


# 健康分扣分权重。P0 是"数据不可用"，必须显著重于"数据有点脏"。
_PENALTY = {Severity.P0: 12.0, Severity.P1: 4.0, Severity.P2: 1.0}


def run_inspection(
    snapshot: Snapshot,
    triage: BaselineTriage | None = None,
) -> InspectionResult:
    """跑一次巡检。纯函数：相同快照 + 相同归因器 → 相同报告。"""
    triage = triage or BaselineTriage()
    ctx = snapshot.to_probe_context()
    findings = run_probes(ctx)
    diagnoses = triage.diagnose_all(findings, ctx)

    by_key = {d.finding_key: d for d in diagnoses}
    alerts: list[Alert] = []
    suppressed: list[tuple[Finding, Diagnosis]] = []

    for f in findings:
        d = by_key.get(f.key)
        if d is None or d.suppressed:
            if d is not None:
                suppressed.append((f, d))
            continue
        route, action = ROUTES.get(d.severity, ("人工判断", "无匹配路由，转入人工复核"))
        alerts.append(Alert(finding=f, diagnosis=d, route=route, action=action))

    ranked = sorted(alerts, key=lambda a: _PENALTY.get(a.diagnosis.severity, 0), reverse=True)
    alerts = _merge_alerts(ranked)

    sev: dict[str, int] = {}
    causes: dict[str, int] = {}
    penalty = 0.0
    for a in alerts:
        s = a.diagnosis.severity.value
        sev[s] = sev.get(s, 0) + 1
        penalty += _PENALTY.get(a.diagnosis.severity, 1.0)
    for _, d in suppressed:
        causes[d.root_cause.value] = causes.get(d.root_cause.value, 0) + 1
    for a in alerts:
        causes[a.diagnosis.root_cause.value] = causes.get(a.diagnosis.root_cause.value, 0) + 1

    n_findings = len(findings)
    n_sup = len(suppressed)
    summary = InspectionSummary(
        asof=snapshot.asof,
        n_symbols=int(snapshot.universe["symbol"].nunique()) if not snapshot.universe.empty else 0,
        n_rows=len(snapshot.stock_daily),
        n_findings=n_findings,
        n_alerts=len(alerts),
        n_suppressed=n_sup,
        by_severity=sev,
        by_root_cause=causes,
        health_score=max(0.0, round(100.0 - penalty, 1)),
        noise_reduction=(n_sup / n_findings) if n_findings else 0.0,
    )
    return InspectionResult(
        asof=snapshot.asof,
        alerts=alerts,
        suppressed=suppressed,
        summary=summary,
    )


def _merge_alerts(alerts: list[Alert]) -> list[Alert]:
    """同一标的的同一根因合并成一条告警。

    上游停更会同时触发"新鲜度"和"完整性"两个探针——它们是同一件事的两种表现。
    不合并的话，一次故障在值班列表里占两行，人很快就学会无视这个列表。
    """
    buckets: dict[tuple[str, str, str], list[Alert]] = {}
    for a in alerts:
        key = (a.finding.table, a.finding.symbol, a.diagnosis.root_cause)
        buckets.setdefault(key, []).append(a)

    out: list[Alert] = []
    for group in buckets.values():
        group.sort(key=lambda a: _PENALTY.get(a.diagnosis.severity, 0), reverse=True)
        head = group[0]
        out.append(
            Alert(
                finding=head.finding,
                diagnosis=head.diagnosis,
                route=head.route,
                action=head.action,
                merged=len(group),
                extra_windows=tuple(a.finding.window for a in group[1:]),
            )
        )
    out.sort(key=lambda a: (_PENALTY.get(a.diagnosis.severity, 0), a.finding.symbol), reverse=True)
    return out


def benign_label(cause: RootCause) -> str:
    """合法事件的中文名，报告里要让人一眼看懂为什么没告警。"""
    return {
        RootCause.BENIGN_SUSPENSION: "停牌",
        RootCause.BENIGN_CORPORATE_ACTION: "除权除息",
        RootCause.BENIGN_NEW_LISTING: "新股上市",
        RootCause.BENIGN_NON_TRADING_DAY: "非交易日",
    }.get(cause, cause.value)
