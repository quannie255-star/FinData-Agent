"""LLM 归因器。

设计上最重要的不是 prompt，而是**不信任模型**：

1. 输出必须是受控 JSON，根因与级别只能取白名单内的值
2. 任何一条不合法、缺失、解析失败 → 该条**降级为规则归因器的结论**
3. 整批调用失败（无 key / 超时 / 限流）→ 全批降级，巡检照常产出

理由很直接：归因层是全系统的判断中枢，让它依赖一个会幻觉的组件，
就必须保证它最坏情况退化成确定性规则，而不是退化成随机噪声。
这样"接 LLM"才是可上线的改动，而不是一个只能演示的玩具。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from findata.agent.llm import LLMClient, LLMError
from findata.dq.memory import fingerprint, value_bucket
from findata.dq.models import DIAGNOSABLE_CAUSES, Diagnosis, Finding, RootCause, Severity
from findata.dq.probes import ProbeContext
from findata.dq.triage import (
    _coverage,
    _failed_run,
    _halted_days,
    _has_event,
    _list_date,
    _parse_window,
)

_BATCH = 8  # 每批信号数。太大容易触发截断，太小浪费往返。

# 同窗口同步放量（对其他标的）判定阈值：近期均量/前 30 日均量超过它算"在放量"。
# 取 3.0 而非探针告警阈值的 5.0：这里要捕捉的是"市场级放量"的存在性，
# 不是给别的标的重新归因。
_PEER_SURGE_RATIO = 3.0

# 当前默认 = v2（GEPA 式反思进化产物，eval/prompts/triage_v2.yaml 归档可回溯）。
# 进化史：v1 手写基线 → v1.1 级别白名单契约修复（反射器诊断、维护者确认）
# → v2 进化产物（真实 DeepSeek 实测：extended+base 双语料 根因/抑制/精确
# 全 100%、判定率 100%，过全量不回归门禁后合入；对比报告见
# examples/eval-prompt-evolution.txt）。v1 的原文在版本档案里，可随时回滚。
_SYSTEM = """你是 A 股数据质量归因专家。

任务：给定若干数据质量信号及其证据，判断每个信号的根因与告警级别。

铁律：
1. 只依据证据判断，不要臆测证据里没有的事实。
2. 缺失类信号（freshness/completeness）先看窗口性质：
   - is_tail_window=true 且 present_days_in_window 明显少于 trading_days_in_window、suspension_coverage=0、failed_ingest_run=null 时，判 upstream_stale（上游停更），不要判 missing_rows。
   - 只有非尾部窗口、且无停牌/非交易日/新股等合法解释时，才判 missing_rows。
3. 合法性缺失（停牌、非交易日、新股上市窗口）判 benign_*。
4. 取值类信号（validity）：
   - 出现非正价格/负值等明显越界值（如 non_positive_close），即使标的为新上市，也判 value_out_of_range，不得用 benign_new_listing 掩盖；新股上市只解释“缺失”，不解释“负值”。
   - pct_chg_out_of_limit 的判定顺序（先查线索，再定根因）：
     a) 只要证据或 note 中出现除权除息/复权因子线索（如 note 含“除权除息”“复权因子”、has_ex_dividend=true、复权因子≠1），即判 benign_corporate_action；note 中的除权除息/复权因子描述同样算有效线索，不得因 has_ex_dividend=false 就忽略 note。
     b) 仅当既无除权除息线索、也无其他合法业务事件线索时，才考虑 benign_market_action。
5. 漂移类信号（drift）：
   - 判 distribution_drift 必须同时满足：存在量纲/单位不一致线索（volume_amount_ratio_shift 明显偏离 1，如 >1.5 或 <0.5），且该线索无法被合法行情解释。
   - 若 volume_amount_ratio_shift 接近 1（如 0.5~1.5 区间），说明量额同向变化，属合法行情特征，不得据此判 distribution_drift。
   - 证据显示量额同涨/同跌、same_window_peers>0 或 note 指向政策行情等合法放量脉冲时，判 benign_market_action。
   - 只有证据明确指向市场行为时才判 benign_market_action。
6. 证据没有明确指向故障时，宁可判 benign_*，但不得用 benign_* 覆盖证据中已出现的越界值、量纲偏移等故障线索。
7. severity 只能取 P0/P1/P2/OK：真实故障按影响取 P0/P1/P2，合法业务事件（benign_*）一律取 OK。禁止自造其他级别。
8. 只输出 JSON，不要任何解释性文字、不要代码块标记。

可选根因（必须原样取自此列表）：
{causes}

输出格式：
{{"results":[{{"index":0,"root_cause":"upstream_stale","severity":"P0","confidence":0.9,"explanation":"一句话说明判断依据"}}]}}"""

_USER = """观察日：{asof}

信号与证据：
{payload}

请逐个判断，index 必须与输入一致，不要遗漏。"""


@dataclass
class TriageStats:
    """归因过程可观测：到底有多少条是真由模型判的。"""

    total: int = 0
    from_llm: int = 0
    fallback: int = 0
    rejected: int = 0  # 模型给了答案但不合法，被拒收
    errors: list[str] = field(default_factory=list)
    # 拒收样本留痕（截取前若干条）：反射式 prompt 进化需要看到模型的
    # 原始非法输出（比如自创 "P3" 级别），否则只知道自己错了不知道错哪
    rejected_samples: list[dict] = field(default_factory=list)
    rejected_samples_cap: int = 8

    @property
    def llm_coverage(self) -> float:
        return self.from_llm / self.total if self.total else 0.0


def build_evidence(finding: Finding, ctx: ProbeContext) -> dict:
    """把一个信号连同它的上下文证据打包成模型可见的事实。

    这一步是 LLM 归因器的真正价值所在：规则归因器用的也是同一批证据，
    因此两者的差异只可能来自"推理"，不会来自"看到的东西不一样"。
    """
    parsed = _parse_window(finding.window)
    ev: dict = {
        "index": 0,
        "probe": finding.probe,
        "table": finding.table,
        "symbol": finding.symbol,
        "window": finding.window,
        "metric": finding.metric,
        "value": finding.value,
        "threshold": finding.threshold,
    }
    if parsed is None:
        return ev
    start, end = parsed

    if finding.probe in ("freshness", "completeness", "history_depth"):
        halted = _halted_days(ctx, finding.symbol)
        ev["suspension_coverage"] = round(_coverage(ctx, start, end, halted), 3)
        ev["is_tail_window"] = end >= ctx.asof
    if finding.probe == "drift" and finding.table == "stock_daily":
        # 两个判别事实，用于分开「单位/口径变更」与「合法放量」——
        # 这对形态在探针比值上同分布（扩充语料的混淆对），阈值无解；
        # 但量额一致性只说谎一边：单位变更只改 volume 一个字段，
        # 真实行情的放量则量、额、换手同步走。
        shift = _vol_amount_ratio_shift(ctx, finding.symbol, start, end)
        if shift is not None:
            ev["volume_amount_ratio_shift"] = round(shift, 3)
        ev["same_window_peers"] = _same_window_peers(ctx, start, end, exclude=finding.symbol)
    ev["has_ex_dividend"] = _has_event(ctx, finding.symbol, "ex_dividend", start, end)
    ld = _list_date(ctx, finding.symbol)
    ev["list_date"] = ld.isoformat() if isinstance(ld, date) else None
    ev["trading_days_in_window"] = len(ctx.calendar.trading_days(start, end))
    ev["present_days_in_window"] = _present_days(ctx, finding, start, end)
    run_id = _failed_run(ctx, finding.symbol, finding.table)
    ev["failed_ingest_run"] = run_id
    return ev


def _memory_extras(ev: dict) -> tuple[str, ...]:
    """从证据条目提取指纹的判别标签——混淆对不 conflated 的关键。

    drift 故障与合法放量在 probe|metric|比值分桶|inner 上同指纹，
    但量额一致性分桶（va_shift ~1 vs ~5+）与同窗口共振（peers/solo）
    不同。这些标签由本模块计算后传给 dq 层的 fingerprint，依赖方向
    保持 dq 不 import agent。
    """
    extras: list[str] = []
    if "volume_amount_ratio_shift" in ev:
        extras.append(f"va_shift:{value_bucket(float(ev['volume_amount_ratio_shift']))}")
    if "same_window_peers" in ev:
        extras.append("peers" if ev["same_window_peers"] else "solo")
    return tuple(extras)


def _present_days(ctx: ProbeContext, finding: Finding, start: date, end: date) -> int:
    frame = ctx.stock_daily if finding.table == "stock_daily" else ctx.valuation_daily
    if frame.empty or "symbol" not in frame.columns:
        return 0
    sub = frame[
        (frame["symbol"] == finding.symbol) & (frame["date"] >= start) & (frame["date"] <= end)
    ]
    return int(len(sub))


def _symbol_window(frame, symbol: str, start: date, end: date | None):
    """某标的在 [start, end]（end=None 表示截止 start 之前）的行情子集。"""
    sub = frame[frame["symbol"] == symbol]
    if end is None:
        return sub[sub["date"] < start].sort_values("date")
    return sub[(sub["date"] >= start) & (sub["date"] <= end)]


def _vol_amount_ratio_shift(ctx: ProbeContext, symbol: str, start: date, end: date) -> float | None:
    """窗口内 量/额 相对前 30 个交易日的变化倍数。

    成交量单位被改（只放大 volume）→ 该比值跳变 ~20x；
    真实放量（量额同步）→ 比值稳定 ~1.0。这是分开「口径变更」与
    「合法放量」的最强单一线索，且完全由数据计算，不引入外部事实。
    """
    frame = ctx.stock_daily
    if frame.empty or not {"volume", "amount"} <= set(frame.columns):
        return None

    def _ratio(df) -> float | None:
        if df.empty:
            return None
        vol = df["volume"].astype(float)
        amt = df["amount"].astype(float).replace(0.0, float("nan"))
        va = (vol / amt).dropna()
        if va.empty or float(va.mean()) <= 0:
            return None
        return float(va.mean())

    window_ratio = _ratio(_symbol_window(frame, symbol, start, end))
    prior_ratio = _ratio(_symbol_window(frame, symbol, start, None).tail(30))
    if window_ratio is None or prior_ratio is None or prior_ratio == 0:
        return None
    return window_ratio / prior_ratio


def _same_window_peers(
    ctx: ProbeContext, start: date, end: date, exclude: str, limit: int = 30
) -> int:
    """同窗口内同步放量的其他标的个数（市场级行情的证据）。

    放量若来自行情（政策、板块事件），会是多标的同窗口共振；
    口径变更几乎总是单点事故。这是第二个独立线索。
    """
    frame = ctx.stock_daily
    if frame.empty or "symbol" not in frame.columns:
        return 0
    peers = 0
    for symbol, g in frame.groupby("symbol"):
        if symbol == exclude:
            continue
        g = g.sort_values("date")
        win = g[(g["date"] >= start) & (g["date"] <= end)]["volume"].astype(float)
        prior = g[g["date"] < start].tail(30)["volume"].astype(float)
        if win.empty or prior.empty or float(prior.mean()) <= 0:
            continue
        if float(win.mean()) / float(prior.mean()) > _PEER_SURGE_RATIO:
            peers += 1
        if peers >= limit:
            break
    return peers


class LLMTriage:
    """LLM 归因器。实现了 Triage 协议，可直接替换 BaselineTriage 跑同一套评测。

    M13 起支持三件套：
    - system/user 模板覆盖（进化产物替换内置 prompt，接口不变）
    - memory（TriageMemory）：已确认的历史先例注入 prompt，归因结论回流记忆
    - 记忆永远只是「参考线索」：注入措辞带框架，输出仍过白名单校验与降级，
      低置信/未确认记忆在召回层就被拦下，不可能改变抑制决策
    """

    def __init__(
        self,
        client: LLMClient,
        fallback=None,
        system: str | None = None,
        user: str | None = None,
        memory=None,
    ) -> None:
        self.client = client
        from findata.dq.triage import BaselineTriage

        self.fallback = fallback or BaselineTriage()
        self.stats = TriageStats()
        self.system = system or _SYSTEM
        self.user = user or _USER
        self.memory = memory

    def diagnose_all(self, findings: list[Finding], ctx: ProbeContext) -> list[Diagnosis]:
        self.stats = TriageStats(total=len(findings))
        if not findings:
            return []

        out: list[Diagnosis] = [self.fallback.diagnose(f, ctx) for f in findings]
        for batch_start in range(0, len(findings), _BATCH):
            batch = list(range(batch_start, min(batch_start + _BATCH, len(findings))))
            self._run_batch(findings, ctx, batch, out)
        # 凡是不是模型判出来的，都算降级——这是衡量 LLM 真实参与度的分子。
        self.stats.fallback = self.stats.total - self.stats.from_llm
        return out

    def _run_batch(
        self,
        findings: list[Finding],
        ctx: ProbeContext,
        indexes: list[int],
        out: list[Diagnosis],
    ) -> None:
        items = []
        for i in indexes:
            ev = build_evidence(findings[i], ctx)
            ev["index"] = i
            items.append({**ev, "evidence": findings[i].evidence})
        payload = json.dumps({"findings": items}, ensure_ascii=False, indent=1)

        user_text = self.user.format(asof=ctx.asof, payload=payload)
        precedents = self._recall_block(findings, items, ctx)
        if precedents:
            user_text += precedents

        try:
            raw = self.client.complete(
                self.system.format(causes=", ".join(DIAGNOSABLE_CAUSES)),
                user_text,
            )
            parsed = self._parse(raw)
        except LLMError as e:
            # 整批失败：保留规则结论，只记录，不打断巡检。
            self.stats.errors.append(str(e))
            return

        for i in indexes:
            item = parsed.get(i)
            if item is None:
                continue  # 模型漏答：保留规则结论，算降级但不算拒收
            diagnosis = self._validate(item, findings[i], ctx)
            if diagnosis is None:
                self.stats.rejected += 1
                if len(self.stats.rejected_samples) < self.stats.rejected_samples_cap:
                    self.stats.rejected_samples.append({"index": i, "output": item})
                continue
            out[i] = diagnosis
            self.stats.from_llm += 1
            if self.memory is not None:
                fp = fingerprint(
                    findings[i],
                    ctx.asof,
                    _memory_extras(next(x for x in items if x["index"] == i)),
                )
                self.memory.observe(fp, diagnosis, ctx.asof, lesson=diagnosis.explanation)

    def _recall_block(self, findings: list[Finding], items: list[dict], ctx: ProbeContext) -> str:
        """把已确认的历史先例渲染成 prompt 附录。任何防线失守都不会走到这里：
        召回层已过滤 pending / 低置信 / 低权重 / 未来条目。"""
        if self.memory is None:
            return ""
        lines: list[str] = []
        seen: set[str] = set()
        for ev in items:
            fp = fingerprint(findings[ev["index"]], ctx.asof, _memory_extras(ev))
            r = self.memory.recall(fp, ctx.asof)
            for row in r.same:
                line = (
                    f"- 同形态 {row['fingerprint']}：过去 {row['hits']} 次判为 "
                    f"{row['root_cause']}（{row['label']}，置信 {row['confidence']:.0%}）"
                    + (f"；{row['lesson']}" if row.get("lesson") else "")
                )
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
            for row in r.cross:
                line = f"- 相关形态 {row['fingerprint']}：{row['lesson']}"
                if line not in seen:
                    seen.add(line)
                    lines.append(line)
        if not lines:
            return ""
        return (
            "\n\n历史先例（跨 run 已确认经验，仅供参考；最终判断必须以本次证据为准）：\n"
            + "\n".join(lines)
        )

    def _parse(self, raw: str) -> dict[int, dict]:
        """从回复里提取 {index: 结论}。容忍代码块包裹与多余文本。"""
        text = raw.strip()
        if text.startswith("```"):
            text = text.strip("`").replace("json", "", 1).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return {}
        try:
            doc = json.loads(text[start : end + 1])
        except ValueError:
            return {}
        rows = doc.get("results") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            return {}
        out: dict[int, dict] = {}
        for row in rows:
            if isinstance(row, dict) and isinstance(row.get("index"), int):
                out[row["index"]] = row
        return out

    def _validate(self, item: dict | None, finding: Finding, ctx: ProbeContext) -> Diagnosis | None:
        """把模型输出校验成可信结论；不合法返回 None，由调用方降级。"""
        if not item:
            return None
        cause_raw = str(item.get("root_cause", "")).strip()
        sev_raw = str(item.get("severity", "")).strip()
        try:
            cause = RootCause(cause_raw)
        except ValueError:
            return None
        try:
            severity = Severity(sev_raw)
        except ValueError:
            return None
        if cause.value not in _ALLOWED_CAUSES:
            return None
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        explanation = str(item.get("explanation", "")).strip()
        if not explanation:
            return None
        return Diagnosis(
            finding_key=finding.key,
            root_cause=cause,
            severity=severity,
            confidence=max(0.0, min(1.0, confidence)),
            explanation=explanation,
            evidence_refs=("llm",),
        )


# UNKNOWN 不允许由模型直接给出：模型说"不知道"没有信息量，
# 这种时候降级给规则归因器反而更可靠。
_ALLOWED_CAUSES = frozenset(DIAGNOSABLE_CAUSES)
