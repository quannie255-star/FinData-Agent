"""字段级引用归因：判断工具返回的某个字段，**在后续消息里被引用了吗**。

这是 R5.1 的核心判定，也是整个 v4.0 里**唯一不能靠规则表解决**的一步：
「模型是否真的用了这个字段」要读模型在后续步骤里的推理与参数，而不是比对文档。

三条设计纪律（每一条都对应一个会让我报错数的陷阱）
----------------------------------------------------

**① 只认"值级"匹配，不认"意思相近"。**
数字、路径、标识符可以精确比对；"意思相近"要靠模型判断，那是另一件事，
不要混在同一个数字里。本模块只做前者——**能精确比对的部分先做干净**。

**② 值在工具调用前就已经在上下文里 → 出现也不算证据。**
这是最容易犯的错：一个值出现在最终回答里，**不等于**模型是从工具返回里读到的。
它可能来自用户提问、来自系统提示、来自上一轮工具返回。所以判据必须是
**「此前不在上下文里」AND「此后出现了」**，两个条件缺一不可。
（只查后者的话，"引用率"会因为共享词汇而系统性虚高。）

**③ 值太短或太常见 → 拒绝下结论，而不是猜。**
`3`、`0`、`src`、`ok` 这类值在任何文本里都会撞上，撞上不构成引用证据。
这类字段的产出必须是 `unverifiable`，**不能计入分子也不能计入分母**——
把它计入分母会把引用率压低，计入分子会抬高，两种都是编数。

归属的强弱分层（写进产物，不只在文档里）
----------------------------------------
- `referenced`：强。值在工具调用**之后**、且此前不在上下文里，出现在模型后续
  消息中。这是"模型确实把这个值用上了"的正证据。
- `not_referenced_this_run`：**弱**。只说明"**本次运行**里没观测到引用"，
  **不说明这个字段无用**——可能只是本批任务没覆盖到它。
  这一条是 R5.0 验收记录 §4.1 与 ROADMAP 五·2 反复强调的那件事：
  **别把"没观测到"写成"无用"**，处置走"删信息不可逆 → 选可逆动作"。

已知能力边界（必须原样写进报告）
--------------------------------
- **中文散文里的引用判不了。** 中文里"引用了某个值"与"描述了同一件事"没有
  可靠的字符串判据，硬做会产出看起来精确、实际是噪声的数字。
  所以本模块**只扫 ASCII 标识符与数字**，中文回答里的引用记为未观测。
- **字段名 ≠ 字段值。** 模型可能提到字段名（"n_lines"）而没用它的值。
  本模块判的是值；字段名出现在后续消息里**不**计为引用。
- **大字段必须先切段再归因，否则会系统性虚高。** 一个装着整个文件内容的字段，
  里面有几百个标识符；模型只要回显**任意一个**，整段就被判成"被引用"，
  而它可能只用了其中 3 行。所以：
  ① 调用方要把大字段切成有意义的片段（如按函数块）再传进来；
  ② `Verdict.n_matched` 与 `FieldValue.chars` 一起看——**"命中 1 个值"撑不起
     "整个 15k 字符的返回被用上了"这句话**。
  ③ 需要更严时抬高 `min_hits`，但**先看 n_matched 再决定**，不要为了让数字
     好看去调阈值。
- 值指纹的归一化（大小写、路径分隔符）会引入少量漏判，宁可漏判不可错判：
  错判会让"引用率"虚高，虚高的数字比偏低的数字危险。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# 可精确比对的值：ASCII 标识符/路径/数字。
# 中文、以及带空格的自由文本**刻意不扫** —— 见模块 docstring 的能力边界。
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.:/<>-]*")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# 撞车概率高、不构成引用证据的值。**加词要慎重**：加错会把真引用判成
# unverifiable（漏判），不加会把噪声判成引用（错判）。错判更危险。
_STOPWORDS = frozenset(
    {
        "true",
        "false",
        "none",
        "null",
        "self",
        "def",
        "class",
        "import",
        "return",
        "print",
        "src",
        "test",
        "tests",
        "py",
        "json",
        "txt",
        "ok",
        "error",
        "not_found",
        "not_implemented",
        "read_file",
        "list_files",
        "search_code",
        "get_signature",
    }
)

STATUS_REFERENCED = "referenced"
STATUS_NOT_REFERENCED = "not_referenced_this_run"
STATUS_UNVERIFIABLE = "unverifiable"

DEFAULT_MIN_LEN = 5
# 纯数字：短到一定程度就没有区分度（page 3 / 第 3 行都可能撞上）。
DEFAULT_MIN_NUM_LEN = 6


@dataclass(frozen=True)
class FieldValue:
    """工具返回的一个字段（或其片段）。"""

    name: str
    value: str
    chars: int = 0

    def __post_init__(self) -> None:
        if not self.chars:
            object.__setattr__(self, "chars", len(self.value))


@dataclass(frozen=True)
class Verdict:
    field: str
    status: str
    reason: str
    matched: tuple[str, ...] = ()
    n_matched: int = 0
    n_candidates: int = 0


# **原生记录**时每个字段最多留多少个指纹（0 = 不截断）。
#
# 它是**存储**约束，不是判定约束 —— 这个区分很关键：
#   · 原生记录要把指纹落进 trace，所以必须封顶；
#   · 重放路径手里有原文，**不该受这个上限影响**。若让上限也作用在重放上，
#     就会静默改写 v0.1 已发布的那些读数（同一份 trace 会算出两个不同的数）。
# 截断方向是**漏判**（模型用的恰好是被砍掉的那个值 ⇒ 判成"未观测到引用"），
# 对"裁剪更好"这个结论不利 ⇒ 安全方向。但必须把 `truncated` 报出来，
# 否则读的人会以为 n_candidates 就是全部可区分值。
MAX_FIELD_TOKENS = 120


@dataclass(frozen=True)
class FieldRecord:
    """工具返回里一个字段的**原生记录**：只有指纹，没有原文。

    `tokens` 已经扣掉了"调用前就在上下文里"的那批值，所以判定 `referenced`
    只需要 `after`，不需要 `before` —— 这正是"可判定是否被引用的**最小信息**"：
    少一个字段判不了，多一个字段就是白存（且是 PII 与体积上的白存）。

    **代价**：`before` 被固化了，事后不能换口径重判。这是拿"可重判"换"不用重放"
    —— 重放要求工具可重放，而真实用户的第三方 MCP 工具未必可重放。
    """

    name: str
    kind: str
    chars: int
    tokens: tuple[str, ...]
    truncated: bool = False
    # 两个**计数**（不是值，所以没有 PII 与体积问题），只为把
    # "判不了"的**成因**说清楚。丢了它们，两种完全不同的成因会被合并成一句话：
    #   ① 值太短/太常见 → 这个值本来就不能当证据（换多少上下文都一样）
    #   ② 调用前已在上下文里 → 它不新，无法把它与既有信息分开（换 `before` 就变）
    # 这两句话给读报告的人的信息完全不同，所以宁可各存一个整数。
    n_preexisting: int = 0
    n_indistinct: int = 0

    @property
    def n_candidates(self) -> int:
        return len(self.tokens)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "chars": self.chars,
            "n_candidates": len(self.tokens),
            "n_preexisting": self.n_preexisting,
            "n_indistinct": self.n_indistinct,
            "truncated": self.truncated,
            "tokens": list(self.tokens),
        }


def fingerprint(value: str, *, min_len: int = DEFAULT_MIN_LEN) -> tuple[str, ...]:
    """值 → 可区分值的指纹（去重、排序、小写）。"""
    return tuple(sorted(t for t in value_tokens(value) if is_distinctive(t, min_len=min_len)))


def make_record(
    name: str,
    kind: str,
    value: str,
    *,
    chars: int = 0,
    before_tokens: frozenset[str] | set[str] = frozenset(),
    min_len: int = DEFAULT_MIN_LEN,
    max_tokens: int = 0,
) -> FieldRecord:
    """从一个字段值造出记录。**"调用前是否已在上下文里"就在这里扣掉。**

    `max_tokens=0`（默认）= 不截断。这是给**重放路径**用的：那条路手里有原文，
    没有存储压力，截断只会白白引入漏判。原生记录由 `fields.record_fields`
    显式传 `MAX_FIELD_TOKENS`。
    """
    all_tokens = value_tokens(value)
    candidates = {t for t in all_tokens if is_distinctive(t, min_len=min_len)}
    preexisting = candidates & set(before_tokens)
    observable = sorted(candidates - preexisting)
    kept = tuple(observable if max_tokens <= 0 else observable[:max_tokens])
    return FieldRecord(
        name=name,
        kind=kind,
        chars=chars or len(value),
        tokens=kept,
        truncated=len(observable) > len(kept),
        n_preexisting=len(preexisting),
        n_indistinct=len(all_tokens) - len(candidates),
    )


@dataclass
class Attribution:
    """一次归因的汇总。**分档计数，不合成一个"利用率"。**

    合成一个数字（referenced / total）正是我在 v3.0 反复拆掉的那种做法：
    它把"我没观测到"和"它没用"压成同一类。这里三个数字分开给。

    另外 `referenced_chars` **不能单独引用**：大字段只要命中一个值就整段计入，
    所以它天然偏高。合法用法是配着 `n_matched` / `n_candidates` 一起看，
    或者先按片段切分再归因。
    """

    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def n_referenced(self) -> int:
        return sum(1 for v in self.verdicts if v.status == STATUS_REFERENCED)

    @property
    def n_not_referenced(self) -> int:
        return sum(1 for v in self.verdicts if v.status == STATUS_NOT_REFERENCED)

    @property
    def n_unverifiable(self) -> int:
        return sum(1 for v in self.verdicts if v.status == STATUS_UNVERIFIABLE)

    def to_dict(self) -> dict[str, object]:
        return {
            "n_referenced": self.n_referenced,
            "n_not_referenced": self.n_not_referenced,
            "n_unverifiable": self.n_unverifiable,
            "note": (
                "三档分开计数，**不合成利用率**："
                "not_referenced 只说明本次运行未观测到引用，不等于无用；"
                "unverifiable 不计入分子也不计入分母。"
                "字段级粒度偏粗（大字段命中一个值即整段计入），"
                "要看 n_matched / n_candidates 才能判断证据有多厚。"
            ),
            "verdicts": [
                {
                    "field": v.field,
                    "status": v.status,
                    "reason": v.reason,
                    "n_matched": v.n_matched,
                    "n_candidates": v.n_candidates,
                    "matched_sample": list(v.matched),
                }
                for v in self.verdicts
            ],
        }


def value_tokens(text: str) -> set[str]:
    """抽出可精确比对的候选值（ASCII 标识符 + 数字），归一化为小写。"""
    tokens = {m.group(0).lower() for m in _IDENT_RE.finditer(text)}
    tokens |= {m.group(0).lower() for m in _NUM_RE.finditer(text)}
    # 标识符正则会把路径里的数字段也吃掉，去掉纯分隔符残留
    return {t for t in tokens if t.strip("._-:/<>")}


def is_distinctive(token: str, *, min_len: int = DEFAULT_MIN_LEN) -> bool:
    """这个值有没有区分度？没有 = 不能拿它的出现当证据。"""
    if token in _STOPWORDS:
        return False
    if token.isdigit():
        return len(token) >= DEFAULT_MIN_NUM_LEN
    return len(token) >= min_len


def judge_record(rec: FieldRecord, *, after: str, min_hits: int = 1) -> Verdict:
    """判一个**原生记录**的字段：值在调用后出现在模型后续消息里了吗。

    唯一需要的"后侧"输入是 `after` —— "前侧"已经在**记录时**扣掉了（见 `FieldRecord`）。
    """
    if not rec.tokens:
        # 成因分开说：这两句话给读报告的人的信息完全不同（见 `FieldRecord`）。
        if rec.n_preexisting and not rec.n_indistinct:
            reason = "该字段的所有可区分值在工具调用前已在上下文里，无法把它与既有信息分开"
        elif rec.n_indistinct and not rec.n_preexisting:
            reason = "值太短或太常见，撞车概率高，出现也不构成引用证据"
        else:
            reason = (
                f"值太短/太常见（{rec.n_indistinct} 个）且有 "
                f"{rec.n_preexisting} 个值调用前已在上下文里 ⇒ 无法构成引用证据"
            )
        return Verdict(rec.name, STATUS_UNVERIFIABLE, reason, n_candidates=0)

    hit = set(rec.tokens) & value_tokens(after)
    suffix = "；指纹被截断，n_candidates 是下界" if rec.truncated else ""
    if len(hit) >= min_hits:
        return Verdict(
            rec.name,
            STATUS_REFERENCED,
            f"值在调用后出现在模型后续消息里（命中 {len(hit)} / 可判 {len(rec.tokens)} 个）"
            + suffix,
            tuple(sorted(hit))[:3],
            n_matched=len(hit),
            n_candidates=len(rec.tokens),
        )
    if hit:
        return Verdict(
            rec.name,
            STATUS_NOT_REFERENCED,
            f"命中 {len(hit)} 个值，低于本次阈值 min_hits={min_hits}，不足以判为引用" + suffix,
            tuple(sorted(hit))[:3],
            n_matched=len(hit),
            n_candidates=len(rec.tokens),
        )
    return Verdict(
        rec.name,
        STATUS_NOT_REFERENCED,
        "本次运行未观测到引用（**不等于该字段无用**，可能只是本批任务没覆盖）" + suffix,
        n_matched=0,
        n_candidates=len(rec.tokens),
    )


def judge_field(
    fv: FieldValue,
    *,
    before: str,
    after: str,
    min_len: int = DEFAULT_MIN_LEN,
    min_hits: int = 1,
) -> Verdict:
    """判一个字段（**有原文**的调用路径，实现上是 `judge_record` 的薄包装）。

    `before` = 本次工具调用**之前**的全部上下文（用户消息 + 历史 + 系统提示）；
    `after`  = 此后模型产出的全部文本（回答 + 后续工具调用参数）。

    `min_hits` = 至少命中几个不同的值才算引用。默认 1；**大字段应当抬高它**，
    否则命中一个标识符就把整段算成被引用（见模块 docstring 的虚高风险）。

    写成薄包装是刻意的：**判定口径只能有一份**。原生记录（`judge_record`）与
    重放路径（`judge_field`）各维护一份逻辑的话，两边迟早会漂出两个数来。
    """
    rec = make_record(
        fv.name,
        "field",
        fv.value,
        chars=fv.chars,
        before_tokens=value_tokens(before),
        min_len=min_len,
    )
    return judge_record(rec, after=after, min_hits=min_hits)


def attribute(
    fields: list[FieldValue],
    *,
    before: str,
    after: str,
    min_len: int = DEFAULT_MIN_LEN,
    min_hits: int = 1,
) -> Attribution:
    return Attribution(
        [
            judge_field(f, before=before, after=after, min_len=min_len, min_hits=min_hits)
            for f in fields
        ]
    )
