"""归因与提案：从 trace 推出「哪些上下文是白带的」。

ROADMAP 交付物定义的 1→2→3 步（观测 / 判定 / 提议）都在这一个模块里，
**没有拆成三个文件是刻意的**：这三步共享同一份口径（什么叫"被引用"、
怎么折算字符、乘几轮），分散到三个文件里，口径迟早会漂。

四条设计纪律，每条都对应一个我已经踩过或差点踩的坑
--------------------------------------------------

**① 工具返回的原文不在 trace 里，但可以重建 —— 而且重建必须被核对。**
`runs` 只记了 `payload_chars`（确定性量）与参数原文，不记返回内容。
好在这些工具是对本地语料的**确定性读取**，所以：拿记录的参数重放一次，
**把重建结果的字符数与记录值对照**，不一致的调用一律降级为
`not_reconstructable`，**退出字段级统计**（不计分子也不计分母）。
没有这道核对，重建就是在给报告编数据。

**② "被引用"必须分层，因为两种证据的强度差一个量级。**
值出现在**后续工具调用的参数**里（结构化、可核对）≫ 值出现在**最终答复的散文**里
（可能只是复述）。所以每条判定同时给出 `as_arg` / `in_answer` 两组命中，
报告里分开列。合成一个"利用率"会把这两类压成同一个数。

**③ 省下的量 = 字符 × 剩余轮数，不是字符 × 1。**
工具返回在**本次调用之后的每一次模型调用**里都要重发。所以一个在第 2 轮产生、
共 8 轮的任务里、值 1000 字符的段，省的是 `1000 × 6`，不是 `1000`。
这个乘数错了，节省量会系统性少报一个数量级——而少报"省了多少"等于把
项目唯一的价值主张算错。

**④ 字符 → token 的换算系数，从**本批 trace**实测出来，并且把离散度一起报。**
每个模型调用同时记了 `prompt_tokens`（provider usage，实测）与
`messages_chars + tools_chars`（本项目数的，确定性）。比值的分布直接告诉我们
换算有多可信；不报分布就等于假装它是精确常数。
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

from findata.contextbudget import attribution
from findata.contextbudget.attribution import FieldValue
from findata.contextbudget.tools import extract_blocks
from findata.economist.trace import RunObs, ToolCallObs, TraceBundle

# 重建一次调用所得内容的索引键
CallKey = tuple[str, int, int]

# 段级"跨工具信息流"扫描的上限：一段里抽出几千个标识符时，全扫对结论没有增益
_MAX_FLOW_TOKENS = 400

_FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*)\n```\Z", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,4} ", re.MULTILINE)


# --------------------------------------------------------------------------
# 1) 观测：把返回内容切成"段"（不切段就没法谈"哪一部分被用了"）
# --------------------------------------------------------------------------


def segment_payload(tool: str, payload: str) -> list[FieldValue]:
    """把一次工具返回切成有意义的段。

    **为什么不按 JSON 字段切**：本项目这批工具的返回是 Markdown 文本块，
    不是结构化 JSON。硬按"字段"切会切出没有语义的碎块，基于它的归因也就
    没有意义。真正有语义的边界是：`read_file` 的 top-level def/class 块、
    `search_code` 的每个命中块、`list_files` 的每个路径。

    **未知结构一律返回空表，不猜。** 空表在统计里读作"结构未观测"，
    绝不读作"没有内容所以可以删"——后者会让报告提议裁掉一个它根本没看懂的返回。
    """
    if not payload:
        return []

    if tool == "list_files":
        return [
            FieldValue(f"path:{line.strip()}", line.strip())
            for line in payload.splitlines()
            if line.strip()
        ]

    if tool == "read_file":
        body = _strip_fence(payload)
        if body is None:
            return []  # 非成功返回（"File not found" 之类）没有块结构
        blocks = extract_blocks(body)
        if blocks:
            return [FieldValue(f"block:{name}", text) for name, _s, _e, text in blocks]
        return _heading_segments(body)

    if tool == "search_code":
        return _search_segments(payload)

    if tool == "get_signature":
        lines = [ln for ln in payload.splitlines() if ln.strip()]
        if not lines or not lines[0].startswith("# "):
            return []
        head = lines[0]
        out = [FieldValue(f"header:{head[:40]}", head)]
        out += [FieldValue(f"sig:{ln.strip()[:60]}", ln) for ln in lines[1:]]
        return out

    return []


def _strip_fence(payload: str) -> str | None:
    match = _FENCE_RE.fullmatch(payload)
    return match.group(1) if match else None


def _heading_segments(body: str) -> list[FieldValue]:
    """没有代码块的文件（如 .md）：按标题切；连标题都没有就整篇一段。"""
    parts = _HEADING_RE.split(body)
    headings = _HEADING_RE.findall(body)
    if not headings or len(parts) < 2:
        head = body.strip().splitlines()[0] if body.strip() else ""
        return [FieldValue(f"whole:{head[:40]}", body)] if body.strip() else []
    out: list[FieldValue] = []
    for idx, part in enumerate(parts[1:], start=1):
        title = part.splitlines()[0].strip() if part.strip() else f"section{idx}"
        out.append(FieldValue(f"section:{title[:40]}", part))
    return out


def _search_segments(payload: str) -> list[FieldValue]:
    lines = payload.splitlines()
    header: list[str] = []
    groups: list[tuple[str, list[str]]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith("### "):
            current = [line]
            groups.append((line[4:].strip(), current))
        elif current is None:
            header.append(line)
        else:
            current.append(line)
    if not groups:
        return []
    out: list[FieldValue] = []
    if header and "".join(header).strip():
        out.append(FieldValue(f"header:{''.join(header).strip()[:40]}", "\n".join(header)))
    out += [FieldValue(f"hit:{name[:60]}", "\n".join(body)) for name, body in groups]
    return out


def segment_kind(name: str) -> str:
    """段名的前缀就是它的种类（`block:` / `hit:` / `path:` / `sig:` / `section:`）。"""
    return name.split(":", 1)[0] if ":" in name else "other"


# --------------------------------------------------------------------------
# 2) 重建（必须核对）
# --------------------------------------------------------------------------


@dataclass
class ReconstructReport:
    """重建的对账结果。**这就是"字段级归因"的准入闸门。**"""

    corpus_root: str = ""
    n_calls: int = 0
    n_ok: int = 0
    n_mismatch: int = 0
    n_skipped_no_root: int = 0
    n_skipped_no_args: int = 0
    mismatches: list[dict[str, object]] = field(default_factory=list)

    @property
    def n_not_reconstructable(self) -> int:
        return self.n_mismatch + self.n_skipped_no_args

    @property
    def coverage(self) -> float:
        return self.n_ok / self.n_calls if self.n_calls else 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "corpus_root": self.corpus_root,
            "n_calls": self.n_calls,
            "n_reconstructed_ok": self.n_ok,
            "n_mismatch": self.n_mismatch,
            "n_skipped_no_corpus_root": self.n_skipped_no_root,
            "n_skipped_unparsable_args": self.n_skipped_no_args,
            "coverage": round(self.coverage, 4),
            "note": (
                "重建 = 用 trace 里记录的参数重放工具，再把结果的字符数与记录的 "
                "payload_chars 对照。**对照通过的调用才进入字段级归因**；"
                "不一致或无参数的一律退出统计，不计分子也不计分母。"
            ),
            "mismatch_examples": self.mismatches[:8],
        }


def reconstruct_calls(
    bundle: TraceBundle, corpus_root: str | Path | None
) -> tuple[dict[CallKey, str], ReconstructReport]:
    """重放每条工具调用，返回 {调用键: 返回内容} 与对账报告。"""
    from findata.contextbudget.tools import call_tool, set_corpus_root

    report = ReconstructReport()
    contents: dict[CallKey, str] = {}

    if corpus_root is None:
        report.n_calls = bundle.n_tool_calls
        report.n_skipped_no_root = report.n_calls
        return contents, report

    set_corpus_root(corpus_root)
    report.corpus_root = str(Path(corpus_root).resolve())

    for call in bundle.all_tool_calls:
        report.n_calls += 1
        if not call.has_arguments:
            report.n_skipped_no_args += 1
            continue
        try:
            result = call_tool(call.name, call.arguments)
        except Exception as exc:  # noqa: BLE001 — 重放失败也要进账，不能吞
            report.n_mismatch += 1
            report.mismatches.append(
                {"task": call.task, "turn": call.turn, "tool": call.name,
                 "reason": f"replay raised {type(exc).__name__}: {exc}"}
            )
            continue
        if result.payload_chars != call.payload_chars:
            report.n_mismatch += 1
            if len(report.mismatches) < 40:
                report.mismatches.append(
                    {
                        "task": call.task,
                        "turn": call.turn,
                        "tool": call.name,
                        "recorded_chars": call.payload_chars,
                        "replayed_chars": result.payload_chars,
                        "reason": "重放字符数与记录不一致（语料变了或工具非确定性）",
                    }
                )
            continue
        report.n_ok += 1
        contents[call_key(call)] = result.content

    return contents, report


def call_key(call: ToolCallObs) -> CallKey:
    return (call.task, call.turn, call.index_in_turn)


# --------------------------------------------------------------------------
# 3) 判定：段级引用归因（分档）+ 跨工具信息流
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class SegmentVerdict:
    task: str
    tool: str
    turn: int
    name: str
    kind: str
    chars: int
    status: str
    n_matched: int
    n_candidates: int
    as_arg: tuple[str, ...] = ()
    in_answer: tuple[str, ...] = ()
    reason: str = ""

    @property
    def referenced(self) -> bool:
        return self.status == attribution.STATUS_REFERENCED


@dataclass
class InfoFlow:
    """跨工具信息流：A 返回的值出现在 B 的**参数**里。

    这是整个工具里**最硬的一条证据**——参数是结构化的、由 trace 原样记录的，
    不存在"散文里提到算不算引用"的解释空间。

    `n_token_hits` = 命中实例数（同一个值被多个后续调用用到会多次计数），
    **不是去重后的不同值个数**——两种数都有用，混用会让人以为信息流更"宽"。
    """

    from_tool: str
    to_tool: str
    n_token_hits: int = 0
    n_segments: int = 0
    samples: list[str] = field(default_factory=list)

    def key(self) -> tuple[str, str]:
        return (self.from_tool, self.to_tool)


@dataclass
class ToolAgg:
    tool: str
    calls: int = 0
    payload_chars: int = 0
    codes: Counter = field(default_factory=Counter)
    n_calls_with_segments: int = 0
    n_calls_no_shape: int = 0
    seg_chars: int = 0
    n_segments: int = 0
    n_referenced: int = 0
    n_not_referenced: int = 0
    n_unverifiable: int = 0
    seg_chars_referenced: int = 0
    seg_chars_not_referenced: int = 0
    seg_chars_unverifiable: int = 0


@dataclass
class CharsToTokens:
    """字符 → token 的实测比值（不是查表来的常数）。"""

    n_turns: int = 0
    total_prompt_tokens: int = 0
    total_chars: int = 0
    per_turn: list[float] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return self.total_prompt_tokens / self.total_chars if self.total_chars else 0.0

    @property
    def median(self) -> float:
        return median(self.per_turn) if self.per_turn else 0.0

    @property
    def p10(self) -> float:
        return _quantile(self.per_turn, 0.10)

    @property
    def p90(self) -> float:
        return _quantile(self.per_turn, 0.90)

    def to_dict(self) -> dict[str, object]:
        return {
            "n_turns": self.n_turns,
            "total_prompt_tokens": self.total_prompt_tokens,
            "total_chars": self.total_chars,
            "tokens_per_char": round(self.ratio, 4),
            "median_tokens_per_char": round(self.median, 4),
            "p10": round(self.p10, 4),
            "p90": round(self.p90, 4),
            "note": (
                "分子是 provider usage 的 prompt_tokens（实测），分母是"
                " messages_chars + tools_chars（本项目数的，确定性）。"
                "比值含 chat 模板与特殊 token 的开销，且随文本语言/代码占比变化，"
                "**用它换算出来的 token 是估算值，不许与实测 token 混用**。"
            ),
        }


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[idx]


@dataclass
class Proposal:
    lever: str
    target: str
    action: str
    evidence: str
    strength: str
    reversible: bool
    affected_calls: int
    saving_chars: int

    def to_dict(self, tokens_per_char: float) -> dict[str, object]:
        return {
            "lever": self.lever,
            "target": self.target,
            "action": self.action,
            "evidence": self.evidence,
            "evidence_strength": self.strength,
            "reversible": self.reversible,
            "affected_calls": self.affected_calls,
            "saving_chars": self.saving_chars,
            "saving_tokens_est": int(round(self.saving_chars * tokens_per_char)),
        }


@dataclass
class Analysis:
    label: str
    bundle: TraceBundle
    reconstruct: ReconstructReport
    tool_aggs: dict[str, ToolAgg] = field(default_factory=dict)
    verdicts: list[SegmentVerdict] = field(default_factory=list)
    flows: dict[tuple[str, str], InfoFlow] = field(default_factory=dict)
    chars_to_tokens: CharsToTokens = field(default_factory=CharsToTokens)
    proposals: list[Proposal] = field(default_factory=list)
    n_calls_attributed: int = 0
    offered_tools: tuple[str, ...] = ()
    tool_list_match_note: str = ""


def analyze(
    bundle: TraceBundle,
    *,
    corpus_root: str | Path | None = None,
    system_prompt: str = "",
    offered: dict[str, list[dict]] | None = None,
) -> Analysis:
    """主流程：重建 → 归因 → 汇总 → 提案。"""
    contents, recon = reconstruct_calls(bundle, corpus_root)
    result = Analysis(label=bundle.label, bundle=bundle, reconstruct=recon)
    result.chars_to_tokens = measure_chars_to_tokens(bundle)

    for run in bundle.runs.values():
        _attribute_run(run, contents, result, system_prompt=system_prompt)

    result.tool_aggs = _aggregate_tools(bundle, result.verdicts)
    result.offered_defs, result.tool_list_match_note = _identify_offered_tools(bundle, offered)
    result.offered_tools = tuple(t["function"]["name"] for t in result.offered_defs)
    result.proposals = _propose(result)
    return result


def _attribute_run(
    run: RunObs,
    contents: dict[CallKey, str],
    result: Analysis,
    *,
    system_prompt: str,
) -> None:
    """对一个任务做有序归因。

    顺序是全部——`before` / `after` 的定义错了，"被引用"这个词就没有意义：
      - `before`  = 系统提示 + 任务 + **此前各轮**的工具参数与返回。
                    **同一轮内的兄弟调用不进 before**：模型是同一条消息里
                    同时发出的它们，谁也看不到谁的结果。
      - `after`   = 最终答复 + **此后各轮**的工具参数。
                    刻意**不含此后的工具返回**：那是工具写的，不是模型写的，
                    把工具的回显算成"模型引用了"会让引用率虚高。
    """
    total_turns = run.n_turns
    prefix: list[str] = [system_prompt, run.task]

    for turn in run.turns:
        later_args = _later_args_by_tool(run, turn.turn)
        before = "\n".join(part for part in prefix if part)
        after_args = "\n".join(later_args.values())
        after_answer = run.final_answer

        for call in turn.tool_calls:
            content = contents.get(call_key(call))
            if content is None:
                continue
            result.n_calls_attributed += 1
            segments = segment_payload(call.name, content)
            if not segments:
                continue
            remaining_turns = max(0, total_turns - turn.turn)
            for verdict in _judge_segments(
                segments,
                tool=call.name,
                task=run.task,
                turn=turn.turn,
                before=before,
                after_args=after_args,
                after_answer=after_answer,
            ):
                result.verdicts.append(verdict)
                _record_flow(verdict, segments, later_args, call, result, remaining_turns)

        for call in turn.tool_calls:
            if call.has_arguments:
                prefix.append(json.dumps(call.arguments, ensure_ascii=False, sort_keys=True))
            content = contents.get(call_key(call))
            if content:
                prefix.append(content)


def _later_args_by_tool(run: RunObs, turn: int) -> dict[str, str]:
    by_tool: dict[str, list[str]] = defaultdict(list)
    for later in run.turns:
        if later.turn <= turn:
            continue
        for call in later.tool_calls:
            if call.has_arguments:
                by_tool[call.name].append(
                    json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
                )
    return {tool: "\n".join(parts) for tool, parts in by_tool.items()}


def _judge_segments(
    segments: list[FieldValue],
    *,
    tool: str,
    task: str,
    turn: int,
    before: str,
    after_args: str,
    after_answer: str,
) -> list[SegmentVerdict]:
    after = after_args + "\n" + after_answer
    out: list[SegmentVerdict] = []
    for fv in segments:
        verdict = attribution.judge_field(fv, before=before, after=after)
        arg_hits, answer_hits = _split_hits(fv.value, before=before, after_args=after_args,
                                            after_answer=after_answer)
        out.append(
            SegmentVerdict(
                task=task,
                tool=tool,
                turn=turn,
                name=fv.name,
                kind=segment_kind(fv.name),
                chars=fv.chars,
                status=verdict.status,
                n_matched=verdict.n_matched,
                n_candidates=verdict.n_candidates,
                as_arg=arg_hits,
                in_answer=answer_hits,
                reason=verdict.reason,
            )
        )
    return out


def _split_hits(
    value: str, *, before: str, after_args: str, after_answer: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """把命中拆成"进了后续调用参数"与"只出现在答复散文里"。

    用的是 `attribution` 的**公开**判据函数（`value_tokens` / `is_distinctive`），
    所以与 `judge_field` 的口径同源，不会各自漂移。
    """
    candidates = {t for t in attribution.value_tokens(value) if attribution.is_distinctive(t)}
    if not candidates:
        return (), ()
    observable = candidates - attribution.value_tokens(before)
    arg_tokens = attribution.value_tokens(after_args)
    arg_hits = observable & arg_tokens
    answer_hits = (observable & attribution.value_tokens(after_answer)) - arg_hits
    return tuple(sorted(arg_hits))[:5], tuple(sorted(answer_hits))[:5]


def _record_flow(
    verdict: SegmentVerdict,
    segments: list[FieldValue],
    later_args: dict[str, str],
    call: ToolCallObs,
    result: Analysis,
    remaining_turns: int,
) -> None:
    if not later_args or remaining_turns <= 0:
        return
    fv = next((s for s in segments if s.name == verdict.name), None)
    if fv is None:
        return
    candidates = [t for t in attribution.value_tokens(fv.value) if attribution.is_distinctive(t)]
    contributed: set[str] = set()
    for token in candidates[:_MAX_FLOW_TOKENS]:
        for to_tool, blob in later_args.items():
            if token in blob:
                flow = result.flows.setdefault(
                    (call.name, to_tool), InfoFlow(from_tool=call.name, to_tool=to_tool)
                )
                flow.n_token_hits += 1
                contributed.add(to_tool)
                if len(flow.samples) < 8 and token not in flow.samples:
                    flow.samples.append(token)
    # n_segments 只在"这一段确实喂给了那个工具"时加一，避免把未命中的段也计入
    for to_tool in contributed:
        result.flows[(call.name, to_tool)].n_segments += 1


def _aggregate_tools(bundle: TraceBundle, verdicts: list[SegmentVerdict]) -> dict[str, ToolAgg]:
    aggs: dict[str, ToolAgg] = {}
    for call in bundle.all_tool_calls:
        agg = aggs.setdefault(call.name, ToolAgg(tool=call.name))
        agg.calls += 1
        agg.payload_chars += call.payload_chars
        agg.codes[call.result_code] += 1

    by_call = defaultdict(list)
    for v in verdicts:
        by_call[(v.task, v.tool)].append(v)

    for agg in aggs.values():
        own = [v for v in verdicts if v.tool == agg.tool]
        agg.n_segments = len(own)
        agg.seg_chars = sum(v.chars for v in own)
        agg.n_referenced = sum(1 for v in own if v.status == attribution.STATUS_REFERENCED)
        agg.n_not_referenced = sum(1 for v in own if v.status == attribution.STATUS_NOT_REFERENCED)
        agg.n_unverifiable = sum(1 for v in own if v.status == attribution.STATUS_UNVERIFIABLE)
        agg.seg_chars_referenced = sum(
            v.chars for v in own if v.status == attribution.STATUS_REFERENCED
        )
        agg.seg_chars_not_referenced = sum(
            v.chars for v in own if v.status == attribution.STATUS_NOT_REFERENCED
        )
        agg.seg_chars_unverifiable = sum(
            v.chars for v in own if v.status == attribution.STATUS_UNVERIFIABLE
        )
        agg.n_calls_with_segments = len({(v.task, v.turn) for v in own})
        agg.n_calls_no_shape = max(0, agg.calls - agg.n_calls_with_segments)
    return aggs


def measure_chars_to_tokens(bundle: TraceBundle) -> CharsToTokens:
    """从本批 trace 实测 chars→token 比值（含离散度）。"""
    measured = CharsToTokens()
    turns = [t for run in bundle.runs.values() for t in run.turns]
    if turns:
        for turn in turns:
            chars = turn.messages_chars + turn.tools_chars
            if chars > 0 and turn.prompt_tokens > 0:
                measured.n_turns += 1
                measured.total_prompt_tokens += turn.prompt_tokens
                measured.total_chars += chars
                measured.per_turn.append(turn.prompt_tokens / chars)
        return measured
    for call in bundle.model_calls:
        chars = call.messages_chars + call.tools_chars
        if chars > 0 and call.prompt_tokens > 0:
            measured.n_turns += 1
            measured.total_prompt_tokens += call.prompt_tokens
            measured.total_chars += chars
            measured.per_turn.append(call.prompt_tokens / chars)
    return measured


# --------------------------------------------------------------------------
# 4) 提议：只提**可执行**且**可逆**的改动
# --------------------------------------------------------------------------


def _identify_offered_tools(
    bundle: TraceBundle, offered: dict[str, list[dict]] | None
) -> tuple[tuple[dict, ...], str]:
    """认出这批 trace 是带着哪一份工具清单跑的 —— 用字符数**核对**，不靠人说。

    判据：某个候选清单的序列化字符数与 trace 里逐轮记录的 `tools_chars` 完全相等。
    相等是确定性的，因此这个识别可以被读报告的人独立复核。
    """
    if not offered:
        return (), "未提供候选工具清单 ⇒ 定义侧提案未生成"
    observed = sorted({t.tools_chars for run in bundle.runs.values() for t in run.turns})
    observed = [c for c in observed if c > 0]
    if not observed:
        return (), "trace 里没有 tools_chars ⇒ 无法识别工具清单，定义侧提案未生成"
    for name, tools in offered.items():
        chars = len(json.dumps(tools, ensure_ascii=False))
        if chars in observed:
            return (
                tuple(tools),
                f"tools_chars 完全匹配（{chars} 字符）⇒ 本批带的是「{name}」清单",
            )
    return (), (
        f"没有任何候选清单的字符数与 trace 记录的 {observed} 相等 ⇒ "
        "无法确定本批用了哪份清单，定义侧提案未生成（**不猜**）"
    )


def _propose(result: Analysis) -> list[Proposal]:
    proposals: list[Proposal] = []
    runs = result.bundle.runs
    total_turns = sum(run.n_turns for run in runs.values())

    # --- 杠杆 A：裁掉从未被调用的工具定义（R5.0 已验证过的那一条）---
    if result.offered_tools:
        called = set(result.tool_aggs)
        silent = [t for t in result.offered_defs if t["function"]["name"] not in called]
        if silent:
            names = [t["function"]["name"] for t in silent]
            # 每个定义的字符数按它自己的 JSON 长度算（不含数组的逗号与方括号，
            # 所以是**偏保守的下界**——省不了比这更多）
            saving = sum(len(json.dumps(t, ensure_ascii=False)) for t in silent) * total_turns
            proposals.append(
                Proposal(
                    lever="定义侧·裁撤未调用工具",
                    target=f"{len(silent)} 个工具：{', '.join(names[:6])}"
                    + ("…" if len(names) > 6 else ""),
                    action="从下发给模型的工具清单里移除，保留实现（随时可加回）",
                    evidence=(
                        f"本批 {len(runs)} 个任务、{total_turns} 轮里"
                        f"这 {len(silent)} 个工具被调用 0 次；"
                        f"每个定义每轮都要重发，故代价 = 各定义字符之和 × 轮数"
                    ),
                    strength="中（「本批没用到」是这一批的观测，不是通用事实）",
                    reversible=True,
                    affected_calls=0,
                    saving_chars=saving,
                )
            )

    # --- 杠杆 B：把「从未被观测到引用」的大段改成按需取（分层加载）---
    by_group: dict[tuple[str, str], list[SegmentVerdict]] = defaultdict(list)
    for v in result.verdicts:
        by_group[(v.tool, v.kind)].append(v)

    for (tool, kind), group in sorted(by_group.items()):
        unused = [
            v for v in group if v.status == attribution.STATUS_NOT_REFERENCED and v.chars >= 200
        ]
        if not unused:
            continue
        saving = sum(
            v.chars * max(0, (runs[v.task].n_turns if v.task in runs else total_turns) - v.turn)
            for v in unused
        )
        proposals.append(
            Proposal(
                lever="返回侧·大段改分层加载",
                target=f"{tool} 的 {kind} 段（{len(unused)} 段 ≥200 字符）",
                action=(
                    "改成「先返回段索引（名字 + 字符数），命中后再取该段全文」——"
                    "**不是删除**：这些段仍可取到，只是不再常驻上下文"
                ),
                evidence=(
                    f"{len(group)} 段里这 {len(unused)} 段在本次运行中**未观测到任何引用**；"
                    f"合计 {sum(v.chars for v in unused):,} 字符，各乘其剩余轮数"
                ),
                strength="弱（`after` 不含模型中间推理文本 ⇒ 会漏判引用；处置取可逆动作）",
                reversible=True,
                affected_calls=len({(v.task, v.turn) for v in unused}),
                saving_chars=saving,
            )
        )

    # --- 杠杆 C：同一参数重复取同一结果（确定性判据，证据最硬）---
    dup_saving = 0
    dup_calls = 0
    dup_tools: Counter = Counter()
    for run in runs.values():
        seen: Counter = Counter()
        for turn in run.turns:
            for call in turn.tool_calls:
                key = (call.name, call.args_hash)
                seen[key] += 1
                if seen[key] > 1 and call.result_code == "ok":
                    dup_calls += 1
                    dup_tools[call.name] += 1
                    dup_saving += call.payload_chars * max(0, run.n_turns - turn.turn)
    if dup_calls:
        proposals.append(
            Proposal(
                lever="调用侧·同参数结果复用",
                target="、".join(f"{t}({n})" for t, n in dup_tools.most_common()),
                action=(
                    "在 agent loop 内按 (工具, 参数摘要) 缓存返回内容，重复调用直接命中缓存"
                    "（**前提：工具是确定性读取**，本项目的读工具满足）"
                ),
                evidence=(
                    f"{dup_calls} 次重复调用：同一 (工具, args_hash) 在一批任务内被再次调用，"
                    f"args_hash 相同是确定性判据"
                ),
                strength="强（参数摘要相同 + 只读工具 ⇒ 结果必然相同）",
                reversible=True,
                affected_calls=dup_calls,
                saving_chars=dup_saving,
            )
        )

    return proposals


# --------------------------------------------------------------------------
# 5) 验证：改前 / 改后两臂对照（ROADMAP 交付物的第 4 步）
# --------------------------------------------------------------------------


@dataclass
class Verification:
    """同一批任务在两臂上的配对对照。

    **为什么必须配对**：两臂的任务集合可能不完全相同（一臂超时、报错、被截断），
    直接比总量会把"少跑了一个任务"记成"变便宜了"。这里按任务文本配对，
    并且把两边的**独有任务数**一起报出来让人看得见。
    """

    grader: str = ""
    n_paired: int = 0
    n_unpaired_before: int = 0
    n_unpaired_after: int = 0
    before_tokens: int = 0
    after_tokens: int = 0
    before_calls: int = 0
    after_calls: int = 0
    before_payload: int = 0
    after_payload: int = 0
    paired_diffs: list[int] = field(default_factory=list)
    sign_p: float = 1.0
    n_pos: int = 0
    n_neg: int = 0
    n_equal: int = 0
    n_graded: int = 0
    before_hits: int = 0
    after_hits: int = 0
    only_before: int = 0
    only_after: int = 0
    mcnemar_p: float = 1.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "grader": self.grader,
            "n_paired": self.n_paired,
            "n_unpaired_before": self.n_unpaired_before,
            "n_unpaired_after": self.n_unpaired_after,
            "before_prompt_tokens": self.before_tokens,
            "after_prompt_tokens": self.after_tokens,
            "delta_prompt_tokens": self.after_tokens - self.before_tokens,
            "before_tool_calls": self.before_calls,
            "after_tool_calls": self.after_calls,
            "before_payload_chars": self.before_payload,
            "after_payload_chars": self.after_payload,
            "n_cheaper": self.n_neg,
            "n_dearer": self.n_pos,
            "n_equal": self.n_equal,
            "sign_test_p": round(self.sign_p, 6),
            "n_graded": self.n_graded,
            "before_hits": self.before_hits,
            "after_hits": self.after_hits,
            "only_before": self.only_before,
            "only_after": self.only_after,
            "mcnemar_exact_p": round(self.mcnemar_p, 6),
            "notes": self.notes,
        }


GRADER_NONE = "none"
GRADER_KEYWORD = "keyword"


def verify(
    before: TraceBundle,
    after: TraceBundle,
    *,
    grader: str = GRADER_NONE,
    before_label: str = "改前",
    after_label: str = "改后",
) -> Verification:
    """按任务文本配对，给出 token / 调用数 / 命中率的对照。"""
    from findata.contextbudget.stats import mcnemar_exact_p, sign_test_p

    v = Verification(grader=grader)
    shared = [task for task in before.runs if task in after.runs]
    v.n_paired = len(shared)
    v.n_unpaired_before = len(before.runs) - len(shared)
    v.n_unpaired_after = len(after.runs) - len(shared)

    for task in shared:
        run_b = before.runs[task]
        run_a = after.runs[task]
        v.before_tokens += run_b.prompt_tokens
        v.after_tokens += run_a.prompt_tokens
        v.before_calls += len(run_b.tool_calls)
        v.after_calls += len(run_a.tool_calls)
        v.before_payload += run_b.payload_chars
        v.after_payload += run_a.payload_chars
        diff = run_a.prompt_tokens - run_b.prompt_tokens
        v.paired_diffs.append(diff)
        if diff > 0:
            v.n_pos += 1
        elif diff < 0:
            v.n_neg += 1
        else:
            v.n_equal += 1

    v.sign_p = sign_test_p(v.n_pos, v.n_neg)

    if grader == GRADER_KEYWORD:
        graded = _grade_keyword(before, after, shared)
        v.n_graded, v.before_hits, v.after_hits = graded[0], graded[1], graded[2]
        v.only_before, v.only_after = graded[3], graded[4]
        v.mcnemar_p = mcnemar_exact_p(v.only_before, v.only_after)
    else:
        v.notes.append(
            f"未提供判分器（--grader {GRADER_KEYWORD} 才判分）⇒ 成功率变化未测得"
        )

    if v.n_unpaired_before or v.n_unpaired_after:
        v.notes.append(
            f"两臂任务集合不完全相同（{before_label}独有 {v.n_unpaired_before}、"
            f"{after_label}独有 {v.n_unpaired_after}）⇒ 只有配对的 {v.n_paired} 个进了对照"
        )
    if v.n_graded and v.n_graded < v.n_paired:
        v.notes.append(
            f"判分器只认得 {v.n_graded} / {v.n_paired} 个任务"
            "（其余任务的答案没有 must_contain 判据）⇒ 命中率只在可判分子集上成立"
        )
    return v


def _grade_keyword(
    before: TraceBundle, after: TraceBundle, shared: list[str]
) -> tuple[int, int, int, int, int]:
    """用任务集自带的 must_contain 判分。**弱判据**，只判关键词出现与否。

    任务文本对不上内置任务集时**不判**（并会被计数暴露出来），不硬套。
    """
    from findata.contextbudget.tasks import TASKS, is_hit

    lookup = {t.question: t for t in TASKS}
    n_graded = before_hits = after_hits = only_before = only_after = 0
    for task in shared:
        spec = lookup.get(task)
        if spec is None:
            continue
        hit_b = is_hit(spec, before.runs[task].final_answer)
        hit_a = is_hit(spec, after.runs[task].final_answer)
        n_graded += 1
        before_hits += int(hit_b)
        after_hits += int(hit_a)
        only_before += int(hit_b and not hit_a)
        only_after += int(hit_a and not hit_b)
    return n_graded, before_hits, after_hits, only_before, only_after
