"""轨迹归因：把一条失败轨迹归到根因，并标出它**危不危险**。

核心区分不是"失败没失败"，而是 **loud 还是 silent**：

  · **loud（吵着失败）**——中断、降级。系统知道自己不知道，于是报错或
    退到护栏。吵，但安全：用户看得见，不会拿错数字去汇报。
  · **silent（静默失败）**——解析出的槽位与事实不符，但链路照常跑完、
    照常出数、门禁照常判"可信"。不吵，**危险**：这正是"SQL 正确但答案
    不该引用"的另一种形态，也是最难被发现的一类。

这个区分是本项目一贯的立场（证据不足就拒答，而不是猜）在 Agent 层的
翻版：会报错的 Agent 是好的 Agent，闷头答错的 Agent 才是坏的。

归因器目前是规则实现，与 dq/triage.py 同构：规则可审计、可单测，
后续换 LLM 时接口不变。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from findata.agentops.schema import STATUS_ERROR, Trace

# ── 类别 ──
CAT_OK = "ok"  # 一路通顺
CAT_GUARD_FLAGGED = "guard_flagged"  # 门禁拦截（不是失败，是护栏生效）
CAT_ABORT_SYMBOL = "abort_symbol"  # 认不出标的
CAT_ABORT_AS_OF = "abort_as_of"  # 缺单日日期槽
CAT_ABORT_RANGE = "abort_range"  # 缺区间槽
CAT_DEGRADE_MONTH = "degrade_month_expansion"  # 问句只给年月，模型不会展开成整月
CAT_DEGRADE_MISSING = "degrade_missing_slots"  # 问句给了完整日期，模型没填进槽位
CAT_MISMATCH = "parse_mismatch"  # 槽位与事实不符却照常出数
CAT_HALLUCINATED = "hallucinated_date"  # 该拒绝的题，编了个前提还照答
CAT_TOOL_MISSING = "tool_missing"  # 调了个不存在的工具
CAT_ENV_TIMEOUT = "env_timeout"  # 沙箱超时——环境的问题，不是 agent 的
CAT_PARAM_ERROR = "param_error"  # 工具参数写错
CAT_RETRY_LOOP = "retry_loop"  # 同一个工具反复调用（死循环）
CAT_STEP_ERROR = "step_error"  # 有硬错误但归不到上面几类
CAT_LUCKY_GUESS = "lucky_guess"  # 结果对了，但路径是脏的（蒙对）

# ── 严重度 ──
SEV_OK = "ok"
SEV_LOUD = "loud"  # 吵着失败，看得见
SEV_SILENT = "silent"  # 静默失败，最危险

_CAT_LABEL = {
    CAT_OK: "通顺",
    CAT_GUARD_FLAGGED: "门禁拦截",
    CAT_ABORT_SYMBOL: "中断·认不出标的",
    CAT_ABORT_AS_OF: "中断·缺日期槽",
    CAT_ABORT_RANGE: "中断·缺区间槽",
    CAT_DEGRADE_MONTH: "降级·问句只给年月，模型不会展开",
    CAT_DEGRADE_MISSING: "降级·有完整日期却漏填槽位",
    CAT_MISMATCH: "静默错·槽位与事实不符",
    CAT_HALLUCINATED: "静默错·编造前提照常出数",
    CAT_TOOL_MISSING: "工具不存在",
    CAT_ENV_TIMEOUT: "沙箱超时（环境问题）",
    CAT_PARAM_ERROR: "工具参数写错",
    CAT_RETRY_LOOP: "重试死循环",
    CAT_STEP_ERROR: "步骤硬错误",
    CAT_LUCKY_GUESS: "蒙对·结果对但路径脏",
}


@dataclass
class Diagnosis:
    run_id: str
    category: str
    severity: str
    evidence: list[str] = field(default_factory=list)
    suggestion: str = ""

    @property
    def label(self) -> str:
        return _CAT_LABEL.get(self.category, self.category)

    @property
    def failed(self) -> bool:
        return self.severity != SEV_OK


def diagnose(t: Trace) -> Diagnosis:
    out = t.outcome
    err = str(out.get("error", ""))
    ev: list[str] = []
    degraded = [s for s in t.steps if s.status == "degraded"]

    for s in degraded:
        ev.append(f"步骤 {s.seq} {s.name} 降级：{s.error}")
    if err:
        ev.append(f"中断：{err}")

    # 1) 中断优先：**一条数字都没给出来**，那就不算静默错。
    #    静默错的定义是"给了答案且答案错了"，没给答案是 loud。
    #    判反了会把"安全地拒绝"误报成"危险地答错"，结论完全相反。
    if err.startswith("aborted:"):
        if "slot_incomplete:as_of" in err:
            cat, sug = CAT_ABORT_AS_OF, "问句里没给出可用的年月日；规则解析不猜年份。"
        elif "slot_incomplete:range" in err:
            cat, sug = CAT_ABORT_RANGE, "问句里没给出完整起止；半截区间会让门禁判错区间。"
        else:
            cat, sug = CAT_ABORT_SYMBOL, "股票池里没有这个名字；补别名表或让用户给代码。"
        return Diagnosis(t.run_id, cat, SEV_LOUD, ev, sug)

    # 2) 硬错误步骤：工具不存在 / 沙箱超时 / 参数写错。看报错原文分类，
    #    不看猜测——报错原文就是证据。
    from findata.agentops.probes import probe_retry_storm, probe_ungrounded_slot

    err_steps = [s for s in t.steps if s.status == STATUS_ERROR]
    if err_steps:
        # 先折叠空白与标点再看关键词：模型/框架报的错不会照着我的关键词写
        # （"ToolNotFoundError"、"timed out"、"no such tool" 都匹配不上
        #  "tool not found" 这种带空格的原文）。折叠后 "tool not found"
        # 与 "ToolNotFoundError" 都变成 "toolnotfound"，一个规则覆盖两种。
        raw_text = " ".join(str(s.error) for s in err_steps).lower()
        flat = re.sub(r"[\s_\-'\"：:（）()]+", "", raw_text)
        if "timeout" in flat or "timedout" in flat or "超时" in flat:
            cat = CAT_ENV_TIMEOUT
            sug = "沙箱超时是环境问题，不该当负样本——重跑一次可能就成了。"
        elif (
            "notfound" in flat
            or "nosuchtool" in flat
            or "unknown" in flat
            or "找不到" in flat
        ):
            cat = CAT_TOOL_MISSING
            sug = "宿主没注册这个工具，属配置问题；但若工具名是模型自己编的，"
            "那又是能力问题——需要比对工具注册表才能定论。"
        elif (
            "参数" in flat
            or "param" in flat
            or "invalid" in flat
            or "argument" in flat
            or "outofrange" in flat
        ):
            cat = CAT_PARAM_ERROR
            sug = "参数写错是 agent 的能力问题，可以当负样本。"
        else:
            cat = CAT_STEP_ERROR
            sug = "归不到已知类别，看 evidence 里的原文。"
        ev += [f"步骤 {s.seq} {s.name} 报错：{s.error}" for s in err_steps]
        return Diagnosis(t.run_id, cat, SEV_LOUD, ev, sug)

    storm = probe_retry_storm(t)["storm"]
    if storm:
        ev += [f"{name} 被调用了 {n} 次" for name, n in storm.items()]
        return Diagnosis(
            t.run_id,
            CAT_RETRY_LOOP,
            SEV_LOUD,
            ev,
            "绕圈子说明它在瞎试，不是在做规划。",
        )

    ungrounded = probe_ungrounded_slot(t)["suspicious"]
    if ungrounded:
        ev += [f"槽位 {x['slot']}={x['value']} 在问句里找不到出处" for x in ungrounded]
        return Diagnosis(
            t.run_id,
            CAT_HALLUCINATED,
            SEV_SILENT,
            ev,
            "模型补了一个用户没给的值，槽位完整所以完整性校验放行。"
            "完整性检查挡不住幻觉——要查的是「这个值有没有出处」，"
            "而这条探针不需要任何 golden 标签，生产环境也能跑。",
        )

    # 1.5) 该拒绝却给出了数字（有 golden 标注时的另一条路）
    if out.get("should_refuse") and not out.get("refused"):
        as_of = _slot_of(t, "as_of")
        ev.append(f"该拒绝的题给出了数字：as_of={as_of}（问句里没有这一天）")
        return Diagnosis(
            t.run_id,
            CAT_HALLUCINATED,
            SEV_SILENT,
            ev,
            "问句只给了月份，模型自己补了一个日期，槽位完整所以校验放行。"
            "完整性检查挡不住幻觉——需要的是「这个值有没有出处」。",
        )

    # 2) 静默错：链路跑完、数字出来了，但取的是另一个口径。
    #    门禁查不出这类——它只管数据健不健康，不管问的是不是这个东西。
    if out.get("parse_matches_golden") is False:
        for s in t.steps:
            if s.name == "parse_query":
                g = t.golden
                r = s.result
                if r.get("symbol") != g.get("symbol"):
                    ev.append(f"symbol 取到 {r.get('symbol')}，应为 {g.get('symbol')}")
                if r.get("intent") != g.get("intent"):
                    ev.append(f"intent 取到 {r.get('intent')}，应为 {g.get('intent')}")
                if r.get("as_of") != g.get("as_of"):
                    ev.append(f"as_of 取到 {r.get('as_of')}，应为 {g.get('as_of')}")
                if r.get("start") != g.get("start"):
                    ev.append(f"start 取到 {r.get('start')}，应为 {g.get('start')}")
                if r.get("end") != g.get("end"):
                    ev.append(f"end 取到 {r.get('end')}，应为 {g.get('end')}")
                break
        return Diagnosis(
            t.run_id,
            CAT_MISMATCH,
            SEV_SILENT,
            ev,
            "链路没断，但取的是另一个区间/口径——门禁不会发现这类错。",
        )

    # 3) 降级：护栏接住了。但若**结果还是对的**，那是蒙对——
    #    路径上走了兜底，教模型学这个等于教它蒙。
    if degraded:
        if out.get("final_correct") is True:
            ev.append("最终结果正确，但路径上走了兜底")
            return Diagnosis(
                t.run_id,
                CAT_LUCKY_GUESS,
                SEV_LOUD,
                ev,
                "结果对了、路径脏了。当正样本会教会模型「蒙也能过」。",
            )
        raw = _raw_of(t)
        if raw:
            ev.append(f"模型原文：{raw[:200]}")
        reasons = [s.error for s in degraded]
        # 「整月展开」只能解释"问句里根本没给具体日"的情况。问句给了
        # 8 月 1 日到 8 月 22 日却还填不出区间，那是模型漏填，不是约定
        # 问题——两者长得很像，混为一谈就会给出错误的改进建议。
        if "区间缺 start/end" in " ".join(reasons) and not _has_day(t.task):
            return Diagnosis(
                t.run_id,
                CAT_DEGRADE_MONTH,
                SEV_LOUD,
                ev,
                "「整月展开成 [月初, 月末]」是本项目约定，不是常识，模型学不到——"
                "该写进提示词，或干脆交给规则解析。",
            )
        return Diagnosis(
            t.run_id,
            CAT_DEGRADE_MISSING,
            SEV_LOUD,
            ev,
            "问句里有完整日期，模型却没填进槽位——是漏填或格式不符，"
            "不是约定问题。换更大模型或收紧输出格式约束。",
        )

    # 4) 门禁拦截：不是失败，是它该干的活
    if out.get("usable") is False or out.get("badge") not in (None, "BASELINE"):
        return Diagnosis(
            t.run_id,
            CAT_GUARD_FLAGGED,
            SEV_OK,
            [f"徽章 {out.get('badge')}：{_msg_of(t)}"],
            "数字不可引用，已给出理由。",
        )

    return Diagnosis(t.run_id, CAT_OK, SEV_OK, ev)


def _has_day(task: str) -> bool:
    """问句里是否给出了具体某一天：**数字**后面跟「日」或「号」。

    不能只看有没有「日」字——「日均成交量」里就有个「日」，会把它误判成
    "问句给了完整日期"，进而把"模型不会展开整月"错归成"模型漏填"，
    给出的改进建议也就跟着错了。
    """
    return bool(re.search(r"\d\s*[日号]", task))


def _raw_of(t: Trace) -> str:
    for s in t.steps:
        if s.name == "parse_query":
            return str(s.result.get("raw", ""))
    return ""


def _slot_of(t: Trace, key: str) -> str:
    for s in t.steps:
        if s.name == "parse_query":
            return str(s.result.get(key))
    return ""


def _msg_of(t: Trace) -> str:
    for s in t.steps:
        if s.name == "trust_check":
            return str(s.result.get("message", ""))[:120]
    return ""


def summarize(traces: list[Trace]) -> dict:
    """多维汇总：按类别、按严重度、按解析器各切一刀。"""
    diags = [diagnose(t) for t in traces]
    by_cat: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    by_parser: dict[str, dict[str, int]] = {}
    for d, t in zip(diags, traces, strict=True):
        by_cat[d.category] = by_cat.get(d.category, 0) + 1
        by_sev[d.severity] = by_sev.get(d.severity, 0) + 1
        p = by_parser.setdefault(t.parser, {"n": 0, "silent": 0, "loud": 0})
        p["n"] += 1
        if d.severity == SEV_SILENT:
            p["silent"] += 1
        elif d.severity == SEV_LOUD:
            p["loud"] += 1
    return {
        "n": len(traces),
        "by_category": by_cat,
        "by_severity": by_sev,
        "by_parser": by_parser,
        "diags": diags,
    }


# ── 第二维：处置（能不能当训练样本）──
#
# 根因与处置是两件事，必须分开：同样是"失败"，沙箱超时的轨迹不该当
# 负样本（环境问题，重跑可能就成），参数写错的才该当。混成一维的话，
# 过滤规则就没法单独调——改处置会动到归因逻辑。
#
# 四档处置：
#   ✓ 正样本   路径干净且结果正确
#   ⚠️ 需人工   结果对但路径可疑，或有疑点
#   ⊘ 不算失败 环境问题/没产出——**别当负样本**，那是浪费数据
#   ✗ 丢弃     agent 能力问题，可以当负样本或丢掉

V_POSITIVE = "✓ 正样本"
V_REVIEW = "⚠️ 需人工"
V_DISCARD = "✗ 丢弃"
V_NOT_FAILURE = "⊘ 不算失败"

_VERDICT = {
    CAT_OK: V_POSITIVE,
    CAT_GUARD_FLAGGED: V_POSITIVE,  # 正确拒绝本身就是好示范
    CAT_LUCKY_GUESS: V_REVIEW,
    CAT_DEGRADE_MONTH: V_REVIEW,
    CAT_DEGRADE_MISSING: V_REVIEW,
    CAT_ABORT_SYMBOL: V_NOT_FAILURE,
    CAT_ABORT_AS_OF: V_NOT_FAILURE,
    CAT_ABORT_RANGE: V_NOT_FAILURE,
    CAT_ENV_TIMEOUT: V_NOT_FAILURE,
    CAT_TOOL_MISSING: V_NOT_FAILURE,
    CAT_PARAM_ERROR: V_DISCARD,
    CAT_RETRY_LOOP: V_DISCARD,
    CAT_STEP_ERROR: V_DISCARD,
    CAT_HALLUCINATED: V_DISCARD,
    CAT_MISMATCH: V_DISCARD,
}


def verdict(d: Diagnosis) -> str:
    """给定根因，给出处置。"""
    return _VERDICT.get(d.category, V_REVIEW)
