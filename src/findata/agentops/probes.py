"""轨迹探针：**只观测，不判断**。

与 `dq/probes.py` 同一条铁律：探针只负责把形态信号摆出来，判定全在
`triage.py`。拆开的理由是判断会换（规则 → LLM → 人工复核），而观测不会；
混在一起，每次改判定都要动探针，回归面就失控了。

五类探针：
  degrade            有步骤降级到兜底
  abort              链路中断，没产出数字
  retry_storm        同一个工具被反复调用（死循环嫌疑）
  ungrounded_slot    槽位的值在问句里找不到出处（幻觉嫌疑）
  silent_mismatch    跑完了，但取的口径与事实不符

其中 `ungrounded_slot` 是这批里最要紧的一个：**它不需要 golden 标签**。
此前抓"模型编了个日期"靠的是"这道题本该拒绝"这个人工标注，生产环境
没有标注，那条路就断了。改成查"这个值在问句里有没有出处"，无标签也能用。
"""

from __future__ import annotations

import json
import re
from datetime import date

from findata.agentops.schema import Trace

# 需要查出处的槽位：日期必须是**用户给的**，不是模型补的。
# symbol 不在此列——它由股票池映射得到，映射本身就是出处。
_GROUNDED_SLOTS = ("as_of", "start", "end")


def probe_degrade(t: Trace) -> dict:
    steps = [s for s in t.steps if s.status == "degraded"]
    return {
        "n": len(steps),
        "names": [s.name for s in steps],
        "errors": [s.error for s in steps],
        "raw": [s.result.get("raw", "") for s in steps],
    }


def probe_abort(t: Trace) -> dict:
    err = str(t.outcome.get("error", ""))
    return {
        "aborted": err.startswith("aborted:"),
        "code": err.split(":", 1)[1] if err.startswith("aborted:") else "",
        "detail": str(t.outcome.get("detail", "")),
    }


def probe_retry_storm(t: Trace, threshold: int = 2) -> dict:
    """同一个工具**带着同样的参数**被调用几次。>= threshold 视为在原地打转。

    判据必须是"同名**且同参数**"，不能只看工具名：真实轨迹里同一个工具带
    不同参数并行调用是常态（查 beta / 查 loot / 查 game 各一次），按名字
    计数会把正常的并行调用全判成死循环——xlam 上第一版就这么误报了 53 条。

    阈值从 3 降到 2：既然已经要求参数完全一致，一次重复调用就已经是"原样
    重试"，而原样重试说明它没从上次结果里学到任何东西。
    """
    counts: dict[str, int] = {}
    identical: dict[str, int] = {}
    seen: dict[tuple[str, str], int] = {}
    for s in t.steps:
        counts[s.name] = counts.get(s.name, 0) + 1
        key = (s.name, json.dumps(s.arguments, sort_keys=True, ensure_ascii=False, default=str))
        seen[key] = seen.get(key, 0) + 1
    for (name, _), n in seen.items():
        if n >= threshold:
            identical[name] = max(identical.get(name, 0), n)
    return {"max_repeats": max(counts.values()) if counts else 0, "storm": identical}


def _mentioned(task: str, value: str) -> bool:
    """值在问句里有没有出处。日期按「年月日三个数字都得出现」判定。"""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", str(value))
    if m:
        y, mo, d = m.groups()
        # 月/日允许 0 填充差异（"9 月" vs "09"），按整数比
        return all(str(int(x)) in task for x in (y, mo, d))
    return str(value) in task


def _has_day(task: str) -> bool:
    return bool(re.search(r"\d\s*[日号]", task))


def _is_month_boundary(s: str) -> bool:
    """是不是某个月的月初/月末——整月展开是项目约定，属于合法推导。"""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if not m:
        return False
    y, mo, d = (int(x) for x in m.groups())
    last = (date(y + (mo == 12), (mo % 12) + 1, 1)).toordinal() - 1
    return d in (1, date.fromordinal(last).day)


def probe_ungrounded_slot(t: Trace) -> dict:
    """槽位值的出处核查：模型有没有自己补了一个用户没给的值。

    这是**无标签也能跑**的幻觉探测——不依赖任何 golden，只看
    "这个值在问句里出现过没有"。查不出处的不是一定有幻觉，但一定有嫌疑。

    两条必须收窄的地方（第一版误报 42/48，全是这两条踩的）：
      · **不查 symbol**：问"中芯国际"却要求问句里出现 688981 是荒谬的，
        symbol 由股票池映射得到，映射本身就是出处；
      · **整月展开放行**：问"8 月的日均成交量"推导出 2025-08-01~08-31，
        那是项目约定（合法推导），不是模型编的。只在问句**给了具体日**
        时才要求每个日期都查得到出处。
    """
    # 问句一个阿拉伯数字都没有（如「二〇二五年九月四日」）：无从核对，
    # 标 unverifiable 而不是指控。沿用本项目「宁可 UNKNOWN 也不编」的
    # 口径——查不出处不等于有幻觉，乱指控比漏检更伤信任。
    if not re.search(r"\d", t.task):
        return {"suspicious": [], "n": 0, "unverifiable": True}

    bad: list[dict] = []
    for s in t.steps:
        if s.name != "parse_query":
            continue
        for key in _GROUNDED_SLOTS:
            v = s.result.get(key)
            if not v or v == "None":
                continue
            # as_of 是单日查询，必须查得到出处；start/end 只在问句给了
            # 具体日时才查，否则整月展开会被误判成幻觉
            if key in ("start", "end") and not _has_day(t.task):
                continue
            if key in ("start", "end") and _is_month_boundary(str(v)):
                continue
            if not _mentioned(t.task, str(v)):
                bad.append({"slot": key, "value": str(v)})
        break
    return {"suspicious": bad, "n": len(bad)}


def probe_silent_mismatch(t: Trace) -> dict:
    """跑完了但取的口径与事实不符。有 golden 才算得出来，没有就是未知。"""
    if "parse_matches_golden" not in t.outcome:
        return {"unknown": True, "mismatched": []}
    if t.outcome.get("parse_matches_golden"):
        return {"unknown": False, "mismatched": []}
    g, mismatched = t.golden, []
    for s in t.steps:
        if s.name == "parse_query":
            for key in _GROUNDED_SLOTS:
                exp = g.get(key)
                got = s.result.get(key)
                if str(exp) != str(got if got is not None else "None"):
                    mismatched.append({"slot": key, "expected": exp, "got": got})
            break
    return {"unknown": False, "mismatched": mismatched}


def run_all(t: Trace) -> dict[str, dict]:
    return {
        "degrade": probe_degrade(t),
        "abort": probe_abort(t),
        "retry_storm": probe_retry_storm(t),
        "ungrounded_slot": probe_ungrounded_slot(t),
        "silent_mismatch": probe_silent_mismatch(t),
    }
