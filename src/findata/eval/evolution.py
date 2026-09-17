"""Prompt 进化引擎（M13）：反思式提示进化 + Pareto 接受 + 门禁把关。

与 ROADMAP 2.1 对应的实现要点：

- **适应度是确定性评测**：扩充语料上的 根因准确率/误报抑制率/告警精确率，
  与 CI 门禁同一把尺子（复用 run_evaluation + metrics.evaluate），
  不是 LLM 自己给自己打的分
- **训练/验收分离**：反射提议只看训练子集的失败案例；候选接受必须
  在全量（extended + base）上不回归。进化的东西能不能用，门禁说了算
- **GEPA 式接受**（Genetic-Pareto 的核心，内置实现零依赖）：候选与
  现任在逐指标上比较，任一指标退化即拒——prompt 进化不允许按下葫芦浮起瓢
- **预算硬顶**：调用计数包装器，--max-llm-calls 到顶即停，开销如实入档

dspy.GEPA 引擎在 scripts/evolve_prompt.py 里作可选路线（evolve 依赖组）；
它产出的候选 instructions 回填本模块的同一套 evaluate/accept 管线——
「谁提议的不重要，过不过门禁才重要」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from findata.agent.llm import LLMClient, LLMError
from findata.agent.llm_triage import LLMTriage, TriageStats, build_evidence
from findata.dq.models import Diagnosis, Finding, RootCause, Severity
from findata.dq.probes import ProbeContext
from findata.eval.metrics import EvalReport, _matches, evaluate
from findata.eval.runner import EvalResult, run_evaluation

# 反射提议的固定契约尾段：候选只改写「判定要领」部分，
# 根因白名单与输出格式由系统注入，不许进化自由发挥（schema 不是措辞）。
SYSTEM_TAIL_MARKER = "可选根因"


@dataclass
class LabeledItem:
    """语料里的一个带标注信号：探针输出 + 标准答案。"""

    finding: Finding
    expected_cause: RootCause
    expected_suppressed: bool
    note: str = ""


@dataclass
class Candidate:
    """一个进化候选：改写后的「判定要领」段 + 评估缓存。"""

    head: str
    rationale: str = ""
    train_score: float | None = None


@dataclass
class CorpusReports:
    """一份 prompt 在两份语料上的评测结果（训练基线与验收共用）。"""

    extended: EvalReport
    base: EvalReport
    extended_result: EvalResult | None = None
    # 扩充语料上的归因过程统计：拒收率高说明输出契约没被遵守，
    # 这是反射提议需要看到的事实（它决定该修判定还是修格式）
    extended_stats: TriageStats | None = None

    @property
    def extended_llm_coverage(self) -> float:
        return self.extended_stats.llm_coverage if self.extended_stats else 0.0


@dataclass
class EvolutionTrace:
    """一次进化全程的可观测记录（报告与版本档案共用）。"""

    rounds: list[dict] = field(default_factory=list)
    accepted_head: str = ""
    accepted_rationale: str = ""
    accepted_reports: CorpusReports | None = None
    baseline_reports: CorpusReports | None = None  # 进化前现任，门禁的对照基线
    llm_calls: int = 0
    llm_chars: int = 0
    stopped_reason: str = ""


class BudgetExhausted(LLMError):
    """LLM 调用预算到顶。进化停止，已产出的候选照常走门禁。"""


class Budget:
    """共享调用预算：评估（温度 0，确定性适应度）与提议（温度 >0，候选
    多样性）分用两个客户端，但花的是同一份预算——开销如实合并记账。"""

    def __init__(self, max_calls: int | None = None) -> None:
        self.max_calls = max_calls
        self.calls = 0
        self.chars = 0

    def charge(self, system: str, user: str) -> None:
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExhausted(f"已达 LLM 调用预算上限（{self.max_calls} 次），进化提前收敛")
        self.calls += 1
        self.chars += len(system) + len(user)


class CountingClient:
    """预算记账的 LLM 客户端包装器。"""

    def __init__(self, inner: LLMClient, budget: Budget) -> None:
        self.inner = inner
        self.budget = budget

    @property
    def available(self) -> bool:
        return self.inner.available

    def complete(self, system: str, user: str) -> str:
        self.budget.charge(system, user)
        return self.inner.complete(system, user)


# ---- 语料与标注 ----


def build_labeled_items(result: EvalResult) -> list[LabeledItem]:
    """把一次评测的 findings 与标注（故障 + 合法事件）对齐成带答案的语料。

    对齐规则与 metrics.evaluate 相同（符号 + 窗口重叠）；窗口同时撞上
    故障与合法事件时以故障标注为准（故障优先，与告警语义一致）。
    """
    items: list[LabeledItem] = []
    claimed: set[str] = set()
    for fault in result.faults:
        for f in result.findings:
            if _matches(f, fault):
                items.append(
                    LabeledItem(
                        f,
                        fault.expected_root_cause,
                        fault.expected_severity is Severity.OK,
                        fault.note,
                    )
                )
                claimed.add(f.key)
    for benign in result.benigns:
        for f in result.findings:
            if f.key in claimed or not _matches(f, benign):
                continue
            items.append(
                LabeledItem(
                    f,
                    benign.expected_root_cause,
                    benign.expected_severity is Severity.OK,
                    benign.note,
                )
            )
            claimed.add(f.key)
    return items


def split_train_held(
    items: list[LabeledItem], result: EvalResult
) -> tuple[list[LabeledItem], list[LabeledItem]]:
    """训练/留存分离：留 2 只放量脉冲标的做未见形态的泛化检验。

    训练集保留 1 只脉冲 + drift 故障——反射要见过混淆对才能提炼判别
    原理；留存集只考「原理迁移到没见过的标的」，防止背答案。
    """
    surge_syms = sorted({b.symbol for b in result.benigns if b.kind == "volume_surge"})
    held_syms = set(surge_syms[1:]) if len(surge_syms) > 1 else set()
    train = [it for it in items if it.finding.symbol not in held_syms]
    held = [it for it in items if it.finding.symbol in held_syms]
    return train, held


def train_accuracy(
    items: list[LabeledItem], dx_by_key: dict[str, Diagnosis], llm_only: bool = True
) -> float:
    """训练集得分：完全命中（根因 + 抑制决策都对）的条目占比。

    llm_only=True（进化选择用）：只给模型亲判的条目记分——诊断的
    evidence_refs 标记了归因来源（LLM 亲判 = ("llm",)）。这不是瞧不起
    规则兜底：兜底是产品行为，全量验收照常给它记功；但**进化的选择
    信号**若把「模型输出被拒收、规则碰巧兜对」记成成功，就会奖励
    高拒收率，掩盖契约不合规这个真正要修的病。
    """
    if not items:
        return 0.0
    hit = 0
    for it in items:
        d = dx_by_key.get(it.finding.key)
        if d is None:
            continue
        if llm_only and d.evidence_refs != ("llm",):
            continue
        if d.root_cause == it.expected_cause and d.suppressed == it.expected_suppressed:
            hit += 1
    return hit / len(items)


def failure_digests(
    items: list[LabeledItem],
    dx_by_key: dict[str, Diagnosis],
    ctx: ProbeContext | None,
) -> list[dict]:
    """训练集失败案例的证据摘要：反射提议的原料。

    摘要里放的是证据字段与期望/实际的根因，不是「你应该怎么改 prompt」——
    怎么改是提议模型的活，喂结论会把它变成抄写员。
    """
    out = []
    for it in items:
        d = dx_by_key.get(it.finding.key)
        if d is None or (
            d.root_cause == it.expected_cause and d.suppressed == it.expected_suppressed
        ):
            continue
        entry: dict = {
            "signal": {
                k: it.finding.__dict__[k]
                for k in ("probe", "table", "symbol", "metric", "value", "threshold")
            },
            "expected": {
                "root_cause": it.expected_cause.value,
                "suppressed": it.expected_suppressed,
            },
            "predicted": {
                "root_cause": d.root_cause.value,
                "suppressed": d.suppressed,
                "explanation": d.explanation,
            },
            "note": it.note,
        }
        if ctx is not None:
            entry["evidence"] = build_evidence(it.finding, ctx)
        out.append(entry)
    return out


# ---- 评估与接受 ----


def run_corpus(
    system: str, user_template: str, extended: bool, fault_seed: int, client: LLMClient
) -> tuple[EvalResult, EvalReport, TriageStats]:
    """一个 prompt 在一份语料上的完整评测（LLM-only 报告 + 归因过程统计）。"""
    llm = LLMTriage(client, system=system, user=user_template)
    result = run_evaluation(fault_seed=fault_seed, extended=extended, second_triage=llm)
    report = evaluate(result.findings, result.llm_diagnoses, result.faults, result.benigns)
    report.naive_alert_precision = result.report.naive_alert_precision
    return result, report, llm.stats


def evaluate_everywhere(
    system: str, user_template: str, fault_seed: int, client: LLMClient
) -> CorpusReports:
    ext_result, rep_ext, ext_stats = run_corpus(system, user_template, True, fault_seed, client)
    _, rep_base, _ = run_corpus(system, user_template, False, fault_seed, client)
    return CorpusReports(
        extended=rep_ext, base=rep_base, extended_result=ext_result, extended_stats=ext_stats
    )


def pareto_accepts(
    challenger: EvalReport,
    incumbent: EvalReport,
    challenger_coverage: float | None = None,
    incumbent_coverage: float | None = None,
) -> bool:
    """Pareto 接受：挑战者逐指标都不差、且至少一项严格更好。

    指标 = 验收三件套（根因准确率 / 误报抑制率 / 告警精确率）+ 模型判定率
    （llm_coverage）。第四项是关键：规则兜底会把「模型变强」掩盖成产品
    指标纹丝不动——现任若靠拒收躲进规则就能在三项上满分，进化将永远
    无法被接受。模型判定率衡量 ROADMAP 定位的「LLM 为辅可测量地变强」；
    它进了接受准则，但不进门禁（门禁只看产品指标不回归）。
    """
    pairs = [
        (challenger.root_cause_accuracy, incumbent.root_cause_accuracy),
        (challenger.benign_suppression, incumbent.benign_suppression),
        (challenger.alert_precision, incumbent.alert_precision),
    ]
    if challenger_coverage is not None and incumbent_coverage is not None:
        pairs.append((challenger_coverage, incumbent_coverage))
    return all(c >= i for c, i in pairs) and any(c > i for c, i in pairs)


def no_regression(challenger: EvalReport, incumbent: EvalReport, tol: float = 1e-9) -> bool:
    """三项产品指标零回归（允许浮点噪声）。对照语料（base）只要求这个。"""
    pairs = (
        (challenger.root_cause_accuracy, incumbent.root_cause_accuracy),
        (challenger.benign_suppression, incumbent.benign_suppression),
        (challenger.alert_precision, incumbent.alert_precision),
    )
    return all(c + tol >= i for c, i in pairs)


def gate_report(
    evolved: CorpusReports, incumbent: CorpusReports, tol: float = 1e-9
) -> tuple[bool, list[str]]:
    """进化门禁（ROADMAP M13 验收门禁 #2）：语料 × 指标 全量不回归。

    evolved / incumbent 为同一 prompt 在两份语料上的报告组。
    返回（是否通过, 失败原因列表）。
    """
    metrics = ("root_cause_accuracy", "benign_suppression", "alert_precision")
    names = {
        "root_cause_accuracy": "根因准确率",
        "benign_suppression": "误报抑制率",
        "alert_precision": "告警精确率",
    }
    failures = []
    for corpus, rep_e, rep_i in (
        ("extended", evolved.extended, incumbent.extended),
        ("base", evolved.base, incumbent.base),
    ):
        for m in metrics:
            e = getattr(rep_e, m)
            i = getattr(rep_i, m)
            if e + tol < i:
                failures.append(f"[{corpus}] {names[m]} {i:.1%} → {e:.1%}（退化）")
    return not failures, failures


# ---- 内置反射进化引擎（GEPA-lite，零依赖） ----

_REFLECTION_SYSTEM = """你是提示词优化专家。给定一个 A 股数据质量归因 LLM 的现行「判定要领」
提示词片段，以及它在训练集上的失败案例（含证据、期望答案、实际回答），
请改写这段提示词，使模型不再犯这些错误，同时不损害它原本判对的部分。

改写纪律：
1. 只允许改写「判定要领」片段。根因白名单与输出格式由系统另行注入，不要写它们。
2. 改进必须来自失败案例中证据与答案的差距，不要臆造证据里没有的判别规则。
3. 保持片段简短（不超过原来三倍），条文可执行、可证伪。
4. 只输出 JSON：{"head": "改写后的判定要领片段", "rationale": "一句话改动要点"}，不要代码块标记。"""


def split_system(system: str) -> tuple[str, str]:
    """把完整 system 模板切成（可进化要领, 固定契约尾段）。"""
    idx = system.find(SYSTEM_TAIL_MARKER)
    if idx < 0:
        raise ValueError(f"system 模板缺少固定契约标记「{SYSTEM_TAIL_MARKER}」")
    return system[:idx].rstrip(), system[idx:]


def assemble_system(head: str, tail: str) -> str:
    return head.rstrip() + "\n\n" + tail


def reflection_user(
    head: str,
    digests: list[dict],
    train_size: int,
    rejected: int = 0,
    total: int = 0,
    rejected_samples: list[dict] | None = None,
    pareto_failures: list[str] | None = None,
) -> str:
    lines = [
        f"现行判定要领：\n{head}\n",
        f"训练集共 {train_size} 条，失败 {len(digests)} 条：",
        json.dumps(digests, ensure_ascii=False, indent=1, default=str),
    ]
    if total and rejected:
        lines.append(
            f"\n另：本轮全量评测 {total} 条信号中，有 {rejected} 条因输出不合法被拒收"
            "（根因/级别不在白名单、缺解释等），拒收即降级为规则结论。"
            "若拒收占比高，说明输出契约本身没被遵守，也应是要领修复的对象。"
        )
        if rejected_samples:
            lines.append(
                "模型被拒收的原始输出样例（注意它们错在哪，比如自创了不存在的级别）：\n"
                + json.dumps(rejected_samples, ensure_ascii=False, indent=1, default=str)
            )
    if pareto_failures:
        lines.append(
            "\n上一版改写虽然在训练集更好，但在全量门禁出现以下退化，"
            "这一版必须修复它们、且不得丢掉已修复的项：\n- "
            + "\n- ".join(pareto_failures)
        )
    lines.append("\n请给出改写后的判定要领（JSON：head / rationale）。")
    return "\n".join(lines)


def parse_proposal(raw: str) -> tuple[str, str] | None:
    """从提议模型的回复里提取 (head, rationale)。解析失败返回 None。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").replace("json", "", 1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        doc = json.loads(text[start : end + 1])
    except ValueError:
        return None
    head = str(doc.get("head", "")).strip()
    if not head:
        return None
    return head, str(doc.get("rationale", "")).strip()


def propose_candidates(
    client: LLMClient,
    head: str,
    digests: list[dict],
    train_size: int,
    k: int,
    rejected: int = 0,
    total: int = 0,
    rejected_samples: list[dict] | None = None,
    pareto_failures: list[str] | None = None,
) -> list[Candidate]:
    """一轮反射提议：k 次独立采样（提供方默认温度带来多样性）。"""
    out: list[Candidate] = []
    user = reflection_user(
        head, digests, train_size, rejected=rejected, total=total,
        rejected_samples=rejected_samples, pareto_failures=pareto_failures,
    )
    for _ in range(k):
        try:
            raw = client.complete(_REFLECTION_SYSTEM, user)
        except LLMError:
            break  # 预算到顶或接口故障：以已有候选收场
        parsed = parse_proposal(raw)
        if parsed is not None:
            out.append(Candidate(head=parsed[0], rationale=parsed[1]))
    return out


def evolve_builtin(
    client: CountingClient,
    propose_client: CountingClient,
    system_head: str,
    system_tail: str,
    user_template: str,
    fault_seed: int,
    rounds: int = 2,
    k_per_round: int = 2,
) -> EvolutionTrace:
    """内置进化主循环：基线 →（提议 → 训练集筛选 → 全量 Pareto 验收）× 轮。

    client 跑评测（温度 0，适应度确定性可复现），propose_client 跑反射
    提议（温度 >0，k 个候选才有多样性），两者共享一份 Budget。
    预算结构：现任基线只评一次（extended+base 两份）；每轮 k 次提议 +
    每候选 1 次训练集评测；只有训练集胜出者才花全量验收（2 份语料）。
    现任被接受替换后，其基线数据直接复用挑战者的评测结果，不重算。
    """
    trace = EvolutionTrace()
    budget = client.budget

    def full_system(head: str) -> str:
        return assemble_system(head, system_tail)

    inc_head = system_head
    inc = evaluate_everywhere(full_system(inc_head), user_template, fault_seed, client)
    trace.baseline_reports = inc
    inc_items = build_labeled_items(inc.extended_result)
    inc_train_items, _held = split_train_held(inc_items, inc.extended_result)
    inc_train = train_accuracy(inc_train_items, _dx_by_key(inc.extended_result))
    trace.rounds.append(
        {"phase": "baseline", "train_score": round(inc_train, 4),
         "train_size": len(inc_train_items)}
    )

    # 反思基准（GEPA 的迭代精炼）：向训练集最优者反思，哪怕它没过全量
    # 验收——下一轮提议能看到「上一个最优候选改坏了什么」，修复才有方向。
    reflect: dict = {
        "head": inc_head,
        "result": inc.extended_result,
        "stats": inc.extended_stats,
        "pareto_failures": None,
    }

    for r in range(rounds):
        digests = failure_digests(
            inc_train_items, _dx_by_key(reflect["result"]), inc.extended_result.ctx
        )
        if not digests:
            trace.stopped_reason = "训练集无失败案例，无可反思对象"
            break
        stats = reflect["stats"]
        cands = propose_candidates(
            propose_client,
            reflect["head"],
            digests,
            len(inc_train_items),
            k_per_round,
            rejected=(stats.rejected if stats else 0),
            total=(stats.total if stats else 0),
            rejected_samples=(stats.rejected_samples if stats else None),
            pareto_failures=reflect["pareto_failures"],
        )
        round_rec: dict = {"round": r + 1, "proposed": len(cands), "candidates": []}
        best: Candidate | None = None
        best_eval: tuple | None = None  # (result, stats) of best candidate
        for cand in cands:
            cand_result, _, cand_stats = run_corpus(
                full_system(cand.head), user_template, True, fault_seed, client
            )
            score = train_accuracy(inc_train_items, _dx_by_key(cand_result))
            cand.train_score = score
            round_rec["candidates"].append(
                {"rationale": cand.rationale, "train_score": round(score, 4)}
            )
            if score > inc_train and (best is None or score > best.train_score):
                best = cand
                best_eval = (cand_result, cand_stats)
        if best is None:
            trace.rounds.append(round_rec)
            trace.stopped_reason = "无候选在训练集上胜过现任，提前收敛"
            break

        cand_reports = evaluate_everywhere(
            full_system(best.head), user_template, fault_seed, client
        )
        # 接受准则：目标语料（extended，含判定率）四指标 Pareto 严格更优；
        # 对照语料（base）只要求产品指标零回归——base 与难例无关，
        # 要求它也「严格更好」等价于禁止一切针对难例的进化。
        accepted = pareto_accepts(
            cand_reports.extended,
            inc.extended,
            challenger_coverage=cand_reports.extended_llm_coverage,
            incumbent_coverage=inc.extended_llm_coverage,
        ) and no_regression(cand_reports.base, inc.base)
        if accepted:
            inc_head, inc, inc_train = best.head, cand_reports, best.train_score
            inc_items = build_labeled_items(inc.extended_result)
            inc_train_items, _ = split_train_held(inc_items, inc.extended_result)
            trace.accepted_head = best.head
            trace.accepted_rationale = best.rationale
            trace.accepted_reports = cand_reports
            round_rec["accepted"] = True
            reflect["pareto_failures"] = None
        else:
            round_rec["accepted"] = False
            # 验收失败明细进反思：下一轮候选必须修复这些退化（val 反馈环）
            _, failures = gate_report(cand_reports, inc)
            reflect["pareto_failures"] = failures
            round_rec["pareto_failures"] = failures
            trace.stopped_reason = "候选未通过全量 Pareto 验收，保留现任"
        # 反思基准推进到训练集最优者（接受与否都推进：下一轮修复它的回归项）
        reflect["head"] = best.head
        reflect["result"] = best_eval[0]
        reflect["stats"] = best_eval[1]
        round_rec["best_rationale"] = best.rationale
        trace.rounds.append(round_rec)

    trace.llm_calls = budget.calls
    trace.llm_chars = budget.chars
    return trace


def _dx_by_key(result: EvalResult) -> dict[str, Diagnosis]:
    return {d.finding_key: d for d in result.llm_diagnoses}
