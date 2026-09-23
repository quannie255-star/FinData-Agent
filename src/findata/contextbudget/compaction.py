"""返回内容裁剪：两种**判断依据完全不同**的裁剪方式，放在同一条对照上比。

R5.0 证明的是**定义侧**的浪费（27 个工具定义里 23 个一次没被调用，占 prompt
的 83.9%/46.4%）。R5.2 要回答的是**返回侧**：工具返回里有多少是白发的？

两种判断依据（这是本模块唯一的自变量）：

1. `head` / `hint` —— **合理性判断**（不看证据）
   只看"这个返回有多长"，按一个**看起来合理**的规则裁：留开头、或整段换成一行提示。
   这正是当下主流做法：上下文里塞不下的时候，摘要一下、截断一下。
   之所以把 `hint` 也做出来，是因为它**是**市面上的极端形态：`tamaratran/fast-jev-compaction`
   的做法是把工具结果整体替换成一行 `ok, 4213 chars (omitted)` 再交给决策模型判断。
   **本模块的 `hint` 是那条协议的仿真，不是 Jev 本体**（Jev 本体闭源、云端、
   要付费，不接进 CI）。仿真与本体差多少：本仿真里"判断者"连内容都看不到，
   这与该协议的公开描述一致；但真实 Jev 有校准过的概率输出，本仿真没有。
   所以本模块**只测"看不到内容的判断"这一条约束的代价**，不测 Jev 的性能。

2. `cap` —— **trace 反推**（只看证据）
   从**已经跑过的一批 trace** 里数出"每次调用模型真正引用了几个段"，
   取 p90 作上限。上限内按位置留，超出的折成一行提示。
   依据是**别处的观测**，不是设计者的直觉——`scripts/build_compact_profile.py`
   负责产出它，产物是 `compaction-profile-*.json`，谁都能拿它复核本模块的行为。

    **上限内的选择按位置是刻意的，也是这套方法的真实边界**：trace 能告诉你
   "该留几个"（统计量），告诉你"该留哪几个"则需要段的**名字**在新调用里重现
   ——`list_files` 的路径名下次未必出现。所以这里不假装有证据。

判据只有一个，两条路共用：**裁剪后的文本进消息，原文本进字段记录**。
这样"哪些字段被引用"与"模型看到了什么"是两个不同的观测，各自都可核对：
  - 字段记录来自**原始**返回（被裁掉的东西也算"观测过但没被引用"）
  - `chars_after` 相对 `chars_before` 的差，就是这一次调用省下的字符（实测）
把这两件事混成一件（例如先裁再记字段），就会得出"裁剪没有损失任何被引用的字段"
这种**同义反复**的结论——因为被裁掉的字段已经不在记录里了。

**不裁的情况必须显式返回原文本，不能返回空串**：
`result_code != "ok"`（一句话的错误返回，没有结构）、未知工具、
以及"段在原文里定位不到"。**定位不到就不裁**，宁可少省，不可乱改——
裁错的代价是模型收到一份它以为完整、其实缺块的上下文。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from findata.contextbudget import fields

# 策略名。**唯一自变量**，账单里必须原样记录它，否则两臂读数无法解释。
POLICY_NONE = "none"
POLICY_HEAD = "head"
POLICY_HINT = "hint"
POLICY_CAP = "cap"
POLICIES = (POLICY_NONE, POLICY_HEAD, POLICY_HINT, POLICY_CAP)

# `head` 保留的头部字符数。**这是个设计者拍的数字**，所以它必须出现在产物里。
# 取 500 的理由只有一条：它大约是"一段代码"的量级，不是"一次工具返回"的量级。
HEAD_CHARS = 500


@dataclass(frozen=True)
class CompactResult:
    """一次裁剪的账。`chars_before` 是**工具返回的原始字符数**（实测）。"""

    policy: str
    text: str
    chars_before: int
    chars_after: int
    n_segments: int = 0
    n_kept: int = 0
    n_mapped: int = 0
    applied: bool = False
    note: str = ""

    @property
    def saved_chars(self) -> int:
        return self.chars_before - self.chars_after

    def to_dict(self) -> dict[str, object]:
        return {
            "policy": self.policy,
            "applied": self.applied,
            "chars_before": self.chars_before,
            "chars_after": self.chars_after,
            "saved_chars": self.saved_chars,
            "n_segments": self.n_segments,
            "n_kept": self.n_kept,
            "n_mapped": self.n_mapped,
            "note": self.note,
        }


def load_profile(path: str | Path) -> dict:
    """读 `build_compact_profile.py` 的产物。**格式不对就抛，不猜。**"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if "caps" not in data or "table" not in data:
        raise ValueError(f"profile 缺少 caps/table 段：{path}")
    return data


def caps_from_profile(profile: dict, *, quantile: str = "p90") -> dict[str, int]:
    """把 profile 的单次引用数分布取成上限表。

    `quantile` 是**必须先定下来的**：p50 会把"真实使用"砍掉一半，p90 只砍尾。
    本实验的正式臂用 p90（保守），p50 只在敏感性检验里出现，且**必须标注**。
    """
    key = f"n_ref_{quantile}"
    caps: dict[str, int] = {}
    for tool, stat in (profile.get("per_call") or {}).items():
        if key not in stat:
            raise ValueError(f"profile.per_call.{tool} 缺少 {key}")
        caps[str(tool)] = max(1, int(stat[key]))
    return caps


def _omit_line(kind: str, n: int, chars: int) -> str:
    """被丢段落的占位行。

    **不许写段值。** 第一版写的是 `-- path:src/c.py (8 chars omitted) --`，
    测试当场抓住两件事：
      · 值又回到了上下文里，`list_files` 这种"值比提示短"的段**裁了等于没裁**；
      · 更糟的是它让"省了多少字符"变成负的——一个以省 token 为卖点的工具，
        自己先把被裁的内容抄回上下文，说不过去。
    所以只留**种类 + 条数 + 字符数**：这三样是账，不是内容。
    """
    unit = f"{n} {kind}"
    return f"-- {unit} ({chars} chars omitted) --"


def _apply_keep(
    payload: str,
    segments: list[tuple[str, int, str]],
    *,
    keep: list[bool],
    trunc: int,
    policy: str,
) -> CompactResult:
    """按 `keep` 逐段改写原文。

    `segments` = [(段名, 字符数, 段原文)]，**顺序必须与它们在原文里出现的顺序一致**。
    定位用 `str.find`，从上一个段的结束位置往后找——这样同名的段不会被互相吃掉。

    定位不到的段（理论上不该有：段就是从原文切出来的）**原样保留并把
    `n_mapped` 记少**；只要 `n_mapped < len(segments)`，报告里就能看见
    "这次裁剪没有按它声称的方式工作"。

    **连续被丢的段合并成一行占位。** 逐段一行看着更"对称"，但在 `list_files`
    这种"值比行短"的返回上，占位行的总长会超过被裁内容的长度——那就不是裁剪，
    是增肥。合并之后一行抵一片，成本才和收益同向。

    **算下来没省就整条回退**（`applied=False`）：宁可交回原文，也不许把一个
    "字符数变小"当成裁剪成功的证据。这个回退在账单里是可见的（`n_ineffective`）。
    """
    out: list[str] = []
    pos = 0
    n_mapped = 0
    n_kept = 0
    pending_kind = ""
    pending_n = 0
    pending_chars = 0

    def flush() -> None:
        nonlocal pending_n, pending_chars
        if pending_n:
            out.append(_omit_line(pending_kind, pending_n, pending_chars) + "\n")
            pending_n = 0
            pending_chars = 0

    for order, (name, _chars, value) in enumerate(segments):
        idx = payload.find(value, pos) if value else -1
        if idx < 0:
            continue
        n_mapped += 1
        out.append(payload[pos:idx])
        if keep[order]:
            flush()
            n_kept += 1
            if trunc and len(value) > trunc:
                out.append(value[:trunc] + f"\n... {len(value) - trunc} chars truncated\n")
            else:
                out.append(value)
        else:
            kind = name.split(":", 1)[0] if ":" in name else "item"
            if kind != pending_kind:
                flush()
                pending_kind = kind
            pending_n += 1
            pending_chars += len(value)
        pos = idx + len(value)
    flush()
    out.append(payload[pos:])
    text = "".join(out)

    if len(text) >= len(payload):
        return CompactResult(
            policy=policy,
            text=payload,
            chars_before=len(payload),
            chars_after=len(payload),
            n_segments=len(segments),
            n_kept=n_kept,
            n_mapped=n_mapped,
            applied=False,
            note=(
                f"裁剪算下来没省（{len(text)} ≥ {len(payload)} 字符）："
                "占位行本身的字符数超过了被裁内容。**整条回退成原文**，"
                "不把这种情形记成一次成功的裁剪。"
            ),
        )

    return CompactResult(
        policy=policy,
        text=text,
        chars_before=len(payload),
        chars_after=len(text),
        n_segments=len(segments),
        n_kept=n_kept,
        n_mapped=n_mapped,
        applied=True,
        note=(
            "逐段改写：留下的段原样（或截断），连续丢掉的段合并成一行占位"
            f"（只记种类/条数/字符数，不写段值）。定位到 {n_mapped}/{len(segments)} 段。"
        ),
    )


def compact(
    tool: str,
    payload: str,
    *,
    policy: str = POLICY_NONE,
    caps: dict[str, int] | None = None,
    result_code: str = "ok",
    head_chars: int = HEAD_CHARS,
) -> CompactResult:
    """把一次工具返回按策略裁成进上下文的文本。

    段的划分**不在这里定义**：`cap` 走 `fields.segment_payload`（与字段记录
    同一个函数）。这是刻意的——如果裁剪与记录各切一份段，"留下的段"和
    "记录的段"就可能对不上，而整个实验的可信度就建立在它们对得上。
    """
    if policy not in POLICIES:
        raise ValueError(f"未知裁剪策略：{policy}（可选 {POLICIES}）")

    before = len(payload)
    if policy == POLICY_NONE:
        return CompactResult(policy, payload, before, before, note="不裁剪")

    if policy == POLICY_HINT:
        # 协议仿真：内容整体换成一行提示。**判断者看不到任何内容**——
        # 这是该协议的核心约束，也正是它和 `cap` 的根本差别。
        segments_hint = fields.segment_payload(tool, payload, result_code=result_code)
        text = (
            f"{tool}: {result_code}, {before} chars, {len(segments_hint)} items "
            "(content omitted; call again with a narrower argument if needed)"
        )
        return CompactResult(
            policy,
            text,
            before,
            len(text),
            n_segments=len(segments_hint),
            n_kept=0,
            applied=True,
            note="整体替换成一行提示：判断者看不到返回内容（协议仿真）",
        )

    if policy == POLICY_HEAD:
        if before <= head_chars:
            return CompactResult(
                policy, payload, before, before, note=f"返回不超过 {head_chars} 字符，未裁",
            )
        text = payload[:head_chars] + (
            f"\n... {before - head_chars} chars omitted (kept the first {head_chars} "
            f"of {before}) ...\n"
        )
        return CompactResult(
            policy,
            text,
            before,
            len(text),
            applied=True,
            note=(
                f"按位置保头部 {head_chars} 字符（设计者定的常数，不是从 trace 学的）；"
                "依据只有「有多长」，没有内容证据"
            ),
        )

    # policy == POLICY_CAP
    cap = int((caps or {}).get(tool, 0) or 0)
    segments = fields.segment_payload(tool, payload, result_code=result_code)
    if cap <= 0 or not segments:
        return CompactResult(
            policy, payload, before, before, n_segments=len(segments),
            n_kept=len(segments),
            note=f"该工具没有上限（cap={cap}）或无结构，未裁",
        )
    return _apply_keep(
        payload,
        [(fv.name, fv.chars, fv.value) for fv in segments],
        keep=[i < cap for i in range(len(segments))],
        trunc=0,
        policy=policy,
    )


def compact_result_json(res: CompactResult) -> str:
    return json.dumps(res.to_dict(), ensure_ascii=False)
