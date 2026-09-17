"""扩充语料（M13）：合法放量脉冲 vs 单位变更故障的「混淆对」。

这是 ROADMAP 难例靶子的语料化。背景：drift 探针算的是 10 日均量/前 30 日
均量，对 ×20 的单位变更注入只报出 ~5.1x（滑动窗口稀释）；而真实政策行情的
放量在探针眼里是同一个数。规则归因器在此形态上没有安全分界——硬塞
「比值 < X 即合法放量」的阈值规则，X 落在两者的比值缝隙里，必然牺牲
一边（实测会使根因准确率 100% → 85.7%）。

本模块在干净基线 + 7 类故障之外，再造一组**合法**的放量脉冲：
- 载体是 3 只无故障、无公司事件的股票（市场级行情，同时起量——
  "同窗口多标的同步放量"本身就是合法性的证据之一）
- 形态与 drift 故障**刻意同构**：同样 ×20、同样持续 10 个交易日——
  但 volume / amount / turnover **同比例**放大（量额换手一致），
  而单位变更只改 volume 一个字段。探针报出的比值因此与故障同分布，
  阈值规则在比值维度上无任何缝隙可插（实测两边都落在 ~5-7x，
  自然波动造成的差异比"合法 vs 故障"的差异还大）
- 脉冲窗口避开数据尾部（结束于观察日之前数日），否则"窗口在尾部"
  会成为「尾部=合法」的投机捷径——真实世界里口径变更恰恰也常在
  尾部被发现，这条捷径会误伤真实故障

刻意简化（如实声明）：放量不抬价。价格判据在合成语料上本就无效
（随机漫步的价格 10 日涨跌 10% 很常见，见 triage.py 已知局限注释），
形态混淆靠量的比值成立，与真实案例（2023-07 券商政策行情）的判别
逻辑一致：看量额一致性与市场同步性，不看涨跌幅。

正确答案：脉冲是 BENIGN_MARKET_ACTION（应抑制），drift 故障是
DISTRIBUTION_DRIFT（应告警）。LLM 归因器若能只凭证据分开这对形态，
就是「自进化产生超越规则引擎能力」的实测证据；分不开，也是诚实的
负结果归档。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from findata.dq.models import RootCause, Severity
from findata.eval.faults import InjectedDataset, InjectedFault, inject_faults
from findata.eval.fixtures import Baseline

# 脉冲参数：与 drift 故障注入同构（×20、10 个交易日），量额换手同涨。
# 这不是巧合而是设计：探针比值两边同分布，阈值规则无缝隙；
# 判别必须依赖 build_evidence 提供的 volume_amount_ratio_shift（~1 vs ~20）
# 与 same_window_peers（>=2 vs 0）。改参数须同步重跑实测与回归断言。
SURGE_FACTOR = 20.0
SURGE_DAYS = 10
SURGE_SYMBOLS = 3
# 脉冲段在数据中的收尾位置：距末尾留 3 个交易日，避免"尾部窗口"捷径
SURGE_TAIL_GAP = 3
SURGE_SEED = 20240915


@dataclass
class ExtendedCorpus:
    """base 语料 + 放量脉冲。surge_benigns 是应当被抑制的合法事件标注。"""

    injected: InjectedDataset
    surge_benigns: list[InjectedFault]


def _surge_pool(baseline: Baseline, fault_symbols: set[str]) -> list[str]:
    """脉冲载体：历史完整、无注入故障、无任何公司事件的股票。"""
    uni = baseline.universe
    if uni.empty:
        return []
    oldest = uni["list_date"].min()
    pool = list(uni.loc[uni["list_date"] == oldest, "symbol"])
    event_symbols: set[str] = set()
    if not baseline.corporate_event.empty:
        event_symbols = set(baseline.corporate_event["symbol"])
    return [s for s in pool if s not in fault_symbols and s not in event_symbols]


def build_extended_corpus(
    baseline: Baseline, fault_seed: int = 20240102, surge_seed: int = SURGE_SEED
) -> ExtendedCorpus:
    """在注入 7 类故障之后，再叠加合法放量脉冲。相同 seed 必得相同结果。"""
    injected = inject_faults(baseline, seed=fault_seed)
    pool = _surge_pool(baseline, {f.symbol for f in injected.faults})
    rng = np.random.default_rng(surge_seed)
    k = min(SURGE_SYMBOLS, len(pool))
    chosen = list(rng.choice(pool, size=k, replace=False))

    daily = injected.stock_daily
    surge_benigns: list[InjectedFault] = []
    for sym in chosen:
        days = sorted(daily.loc[daily["symbol"] == sym, "date"].tolist())
        if len(days) < 40 + SURGE_DAYS:
            continue
        span = days[-(SURGE_DAYS + SURGE_TAIL_GAP) : len(days) - SURGE_TAIL_GAP]
        mask = (daily["symbol"] == sym) & (daily["date"].isin(span))
        daily.loc[mask, "volume"] = daily.loc[mask, "volume"] * SURGE_FACTOR
        daily.loc[mask, "amount"] = daily.loc[mask, "amount"] * SURGE_FACTOR
        # 换手率同步放大，但物理上封顶 100%
        daily.loc[mask, "turnover"] = (daily.loc[mask, "turnover"] * SURGE_FACTOR).clip(upper=100.0)
        surge_benigns.append(
            InjectedFault(
                kind="volume_surge",
                table="stock_daily",
                symbol=sym,
                window=f"{span[0]}..{span[-1]}",
                expected_root_cause=RootCause.BENIGN_MARKET_ACTION,
                expected_severity=Severity.OK,
                note=f"合法放量脉冲（政策行情，量额同涨 ×{SURGE_FACTOR:g}，{SURGE_DAYS} 日）",
            )
        )

    return ExtendedCorpus(injected=InjectedDataset(
        stock_daily=daily,
        valuation_daily=injected.valuation_daily,
        faults=injected.faults,
    ), surge_benigns=surge_benigns)
