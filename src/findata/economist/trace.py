"""读**真实** trace —— 这是本工具与外界的唯一接口。

输入有两种，**能力不同，报告里必须分开说**（混在一起就等于虚报能力）：

1. **OTel span（标准层）**：符合 GenAI 语义约定的 `gen_ai.chat` / `gen_ai.tool.call`。
   任何 agent 只要按约定打点就能喂进来。它给出：工具名、`args_hash`（参数摘要）、
   `result_code`、`payload_chars`、每次模型调用的 token。
   **它给不出**：工具参数**原文**、最终答复、任务文本、逐轮上下文大小。
   ⇒ 只喂 span 时，本工具只能做**全局工具级**归因；字段级归因**未观测**。

2. **runs（增强层）**：本项目 runner 的富记录，补上参数原文、逐轮字符数、
   `final_answer`、任务文本。
   ⇒ 有了它才能做**逐任务、有序、字段级**的归因与"改前/改后"对照。

**为什么要有"增强层"这个概念而不是只支持一种输入**：本工具的对外价值在于
能读**别人的** trace（ROADMAP 交付物里写的就是"读一个 agent 的真实 trace"）。
标准层保证这个通用性；增强层保证我们自己的实验能被完整分析。
两种输入各自能支撑到哪一步，由 `TraceBundle.gaps` **在数据层就声明**，
不留给报告作者临场决定——凭印象写"能力边界"正是本项目反复踩过的坑。

**已观测到的一个结构缺口（本工具能证明，且可被证伪）**：本项目 runner 的 span
**没有 run 级父 span**，所以 span 之间无法归到同一任务。判据是
`n_distinct_trace_ids == n_tool_spans`——每个工具调用都是自己的 trace。
报告里会打印这两个数，读的人可以自己判断我的说法对不对。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPAN_CHAT = "gen_ai.chat"
SPAN_TOOL = "gen_ai.tool.call"

# 标准属性名（GenAI 语义约定）
ATTR_TOOL_NAME = "gen_ai.tool.name"
ATTR_TOOL_ARGS_HASH = "gen_ai.tool.args_hash"
ATTR_TOOL_RESULT_CODE = "gen_ai.tool.result_code"
ATTR_TOOL_DURATION = "gen_ai.tool.duration_ms"
ATTR_MODEL = "gen_ai.request.model"
ATTR_INPUT_TOKENS = "gen_ai.usage.input_tokens"
ATTR_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"

# 本项目扩展属性（约定里没有，故意用自有前缀，不冒充标准）
ATTR_PAYLOAD_CHARS = "findata.context.tool_payload_chars"
ATTR_TURN = "findata.context.turn"
ATTR_MESSAGES_CHARS = "findata.context.messages_chars"
ATTR_TOOLS_CHARS = "findata.context.tools_chars"
ATTR_SEMCONV = "findata.context.semconv"


@dataclass(frozen=True)
class ToolCallObs:
    """一次工具调用（有序分析的最小单位）。"""

    task: str
    turn: int
    index_in_turn: int
    name: str
    call_id: str
    arguments: dict[str, Any]
    args_hash: str
    result_code: str
    payload_chars: int
    duration_ms: float
    parse_error: str = ""

    @property
    def has_arguments(self) -> bool:
        """参数原文是否可用。parse_error 的调用参数已被丢弃，不算可用。"""
        return not self.parse_error and self.arguments is not None


@dataclass
class TurnObs:
    turn: int
    prompt_tokens: int
    completion_tokens: int
    messages_chars: int
    tools_chars: int
    duration_ms: float
    tool_calls: list[ToolCallObs] = field(default_factory=list)


@dataclass
class RunObs:
    """一个任务的完整记录。`task` 就是它的 key（两臂之间任务文本相同即为配对）。"""

    task: str
    final_answer: str
    stopped_reason: str
    turns: list[TurnObs] = field(default_factory=list)
    error: str = ""

    @property
    def n_turns(self) -> int:
        return len(self.turns)

    @property
    def tool_calls(self) -> list[ToolCallObs]:
        return [c for t in self.turns for c in t.tool_calls]

    @property
    def prompt_tokens(self) -> int:
        """逐轮求和。口径与 `stats.prompt_tokens` 一致——**不许两处各解释一次**。"""
        return sum(t.prompt_tokens for t in self.turns)

    @property
    def completion_tokens(self) -> int:
        return sum(t.completion_tokens for t in self.turns)

    @property
    def payload_chars(self) -> int:
        return sum(c.payload_chars for c in self.tool_calls)


@dataclass
class TraceBundle:
    """一个 trace 集合 = 一次"改前"或一次"改后"。"""

    label: str
    source_runs: str = ""
    source_spans: str = ""
    runs: dict[str, RunObs] = field(default_factory=dict)
    model_calls: list[ModelCall] = field(default_factory=list)
    tool_spans: list[dict[str, Any]] = field(default_factory=list)
    semconv: str = ""
    models: tuple[str, ...] = ()
    gaps: list[str] = field(default_factory=list)

    # --- 输入层能力（写在这里，报告照抄，避免报告作者凭印象写边界）---
    @property
    def has_runs(self) -> bool:
        return bool(self.runs)

    @property
    def has_spans(self) -> bool:
        return bool(self.tool_spans) or bool(self.model_calls)

    @property
    def all_tool_calls(self) -> list[ToolCallObs]:
        return [c for run in self.runs.values() for c in run.tool_calls]

    @property
    def n_tool_calls(self) -> int:
        return len(self.all_tool_calls)

    @property
    def n_distinct_trace_ids(self) -> int:
        return len({s.get("trace_id") for s in self.tool_spans if s.get("trace_id")})

    @property
    def has_run_level_parent_span(self) -> bool | None:
        """span 之间能不能归到同一个任务？

        `n_distinct_trace_ids == n_tool_spans` ⇒ 每个工具调用自成一根 trace，
        没有共同的父，**无法逐任务归因**。返回 `None` 表示无法判断（没有 span）。
        """
        if not self.tool_spans:
            return None
        return self.n_distinct_trace_ids < len(self.tool_spans)


@dataclass(frozen=True)
class ModelCall:
    turn: int
    prompt_tokens: int
    completion_tokens: int
    messages_chars: int
    tools_chars: int
    model: str = ""


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped:
            yield json.loads(stripped)


def _looks_like_span(record: dict[str, Any]) -> bool:
    return "attributes" in record


def _looks_like_run(record: dict[str, Any]) -> bool:
    return "turns" in record


def _pick(dir_path: Path, kind: str, tag: str) -> Path | None:
    """在目录里挑一个文件。**三级判据，绝不静默猜。**

    1. 给了 `--tag` 且存在 `runs-<tag>.jsonl` → 用它（**精确优先**）。
    2. 否则按子串匹配；只命中一个才用。
    3. 命中多个（或没给 tag 却有多个）→ **报错并列出候选**。

    第 1 级是必需的：`all7b` 是 `all7b-fz` 的子串，纯子串匹配会同时命中两者
    ——实测踩到过。静默挑一个"最新的"会让报告的输入变成一个没人核对过的选择。
    """
    all_candidates = sorted(dir_path.glob(f"{kind}-*.jsonl"))
    if not all_candidates:
        return None
    if tag:
        exact = dir_path / f"{kind}-{tag}.jsonl"
        if exact.is_file():
            return exact
        candidates = [p for p in all_candidates if tag in p.name]
        if not candidates:
            return None
    else:
        candidates = all_candidates
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise ValueError(
            f"{dir_path} 下有 {len(candidates)} 个候选（{names}），"
            f"不替你猜用哪一个：请用 --tag 精确指定（例如 --tag all7b-fz）"
        )
    return candidates[0]


def load_bundle(target: str | Path, *, tag: str = "", label: str = "") -> TraceBundle:
    """读一份 trace。

    `target` 可以是单个 .jsonl，也可以是目录（目录下按 `runs-*.jsonl` /
    `spans-*.jsonl` 发现）。记录按**内容**分派（有 `turns` 的是 run，有
    `attributes` 的是 span），不看文件名——文件名是人取的，内容不是。
    """
    path = Path(target)
    label = label or (path.name if path.is_file() else path.as_posix())
    bundle = TraceBundle(label=label)

    files: list[Path] = []
    if path.is_dir():
        for kind in ("runs", "spans"):
            picked = _pick(path, kind, tag)
            if picked:
                files.append(picked)
    elif path.is_file():
        files.append(path)
    else:
        raise FileNotFoundError(f"trace 不存在：{path}")

    n_parse_errors = 0
    for file in files:
        if "-spans" in file.name or "span" in file.stem:
            bundle.source_spans = bundle.source_spans or file.as_posix()
        if "-runs" in file.name or "run" in file.stem:
            bundle.source_runs = bundle.source_runs or file.as_posix()
        for record in _iter_jsonl(file):
            if _looks_like_run(record):
                run = _run_from_record(record)
                n_parse_errors += sum(1 for c in run.tool_calls if c.parse_error)
                bundle.runs[run.task] = run
            elif _looks_like_span(record):
                _absorb_span(bundle, record)

    bundle.models = tuple(sorted({c.arguments.get("_model", "") for c in []} or set()))
    bundle.models = tuple(sorted({c.model for c in bundle.model_calls if c.model}))
    if bundle.has_runs and not bundle.source_runs:
        bundle.source_runs = "(由 --trace 直接指定的文件)"
    if bundle.has_spans and not bundle.source_spans:
        bundle.source_spans = "(由 --trace 直接指定的文件)"

    _declare_gaps(bundle, n_parse_errors)
    return bundle


def _run_from_record(record: dict[str, Any]) -> RunObs:
    turns: list[TurnObs] = []
    for turn in record.get("turns", []):
        calls: list[ToolCallObs] = []
        for idx, call in enumerate(turn.get("tool_calls", []), start=1):
            calls.append(
                ToolCallObs(
                    task=str(record.get("task", "")),
                    turn=int(turn.get("turn", 0)),
                    index_in_turn=idx,
                    name=str(call.get("name", "")),
                    call_id=str(call.get("call_id", "")),
                    arguments=call.get("arguments") or {},
                    args_hash=str(call.get("args_hash", "")) or _hash_arguments(call),
                    result_code=str(call.get("result_code", "")),
                    payload_chars=int(call.get("payload_chars", 0)),
                    duration_ms=float(call.get("duration_ms", 0.0)),
                    parse_error=str(call.get("parse_error", "")),
                )
            )
        turns.append(
            TurnObs(
                turn=int(turn.get("turn", 0)),
                prompt_tokens=int(turn.get("prompt_tokens", 0)),
                completion_tokens=int(turn.get("completion_tokens", 0)),
                messages_chars=int(turn.get("messages_chars", 0)),
                tools_chars=int(turn.get("tools_chars", 0)),
                duration_ms=float(turn.get("duration_ms", 0.0)),
                tool_calls=calls,
            )
        )
    return RunObs(
        task=str(record.get("task", "")),
        final_answer=str(record.get("final_answer", "")),
        stopped_reason=str(record.get("stopped_reason", "")),
        turns=turns,
        error=str(record.get("error", "")),
    )


def _hash_arguments(call: dict[str, Any]) -> str:
    """runs 里没记 args_hash（旧批次）时按同一口径补算，保证去重判据一致。"""
    from findata.contextbudget.telemetry import args_hash

    return args_hash(call.get("arguments") or {})


def _absorb_span(bundle: TraceBundle, record: dict[str, Any]) -> None:
    attrs = record.get("attributes") or {}
    name = record.get("name")
    if attrs.get(ATTR_SEMCONV) and not bundle.semconv:
        bundle.semconv = str(attrs[ATTR_SEMCONV])
    if name == SPAN_CHAT:
        bundle.model_calls.append(
            ModelCall(
                turn=int(attrs.get(ATTR_TURN, 0) or 0),
                prompt_tokens=int(attrs.get(ATTR_INPUT_TOKENS, 0) or 0),
                completion_tokens=int(attrs.get(ATTR_OUTPUT_TOKENS, 0) or 0),
                messages_chars=int(attrs.get(ATTR_MESSAGES_CHARS, 0) or 0),
                tools_chars=int(attrs.get(ATTR_TOOLS_CHARS, 0) or 0),
                model=str(attrs.get(ATTR_MODEL, "") or ""),
            )
        )
    elif name == SPAN_TOOL:
        bundle.tool_spans.append(record)


def _declare_gaps(bundle: TraceBundle, n_parse_errors: int) -> None:
    """把**输入层**的缺口写在数据层，报告照抄。

    这样"能力边界"就不再依赖报告作者的诚实程度，而是从输入推导出来的。
    """
    if not bundle.has_runs:
        bundle.gaps.append(
            "未提供 runs 增强层：拿不到工具参数原文、最终答复、逐轮上下文大小 ⇒ "
            "**字段级归因与逐任务对照未观测**，本报告只有全局工具级读数"
        )
    if not bundle.has_spans:
        bundle.gaps.append(
            "未提供 OTel span：拿不到 `args_hash` 与标准结果码 ⇒ 重复调用去重判据退化为参数原文"
        )
    if n_parse_errors:
        bundle.gaps.append(
            f"有 {n_parse_errors} 次调用的参数无法解析（模型给了非法 JSON）⇒ "
            f"这些调用**不参与**字段级归因，且不计入其分母"
        )
    parent = bundle.has_run_level_parent_span
    if parent is False:
        bundle.gaps.append(
            "span 之间**没有 run 级父 span**"
            f"（{len(bundle.tool_spans)} 个工具 span 对应 "
            f"{bundle.n_distinct_trace_ids} 个不同 trace_id）⇒ "
            "只用 span 无法把调用归到具体任务。这是**被观测系统**的打点缺口，不是读法问题；"
            "本工具需要 `gen_ai.invoke_agent`（或等价）级父 span 才能做逐任务归因"
        )
    if bundle.has_runs:
        bundle.gaps.append(
            "trace 里不含**系统提示**与模型的中间推理文本 ⇒ 重建的 `before` 缺系统提示"
            "（可能把「本就在系统提示里」的值误判成新信息），"
            "`after` 只含最终答复与后续调用参数（**会漏判**引用，方向单一）"
        )
