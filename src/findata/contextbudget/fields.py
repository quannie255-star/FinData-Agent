"""字段级观测：把工具返回切成"字段"，并只留下判定引用所需的**最小信息**。

为什么要有这一层（R5.1 的前置，`docs/r5.0-acceptance.md` §4.1）
----------------------------------------------------------
R5.0 的 runner 只记 `payload_chars`（一个数），不记返回内容。于是
"这个字段被引用了没有"**无从回答**——因为根本没有"字段"这个粒度。

第一版解法是**重放**：拿 trace 里记的参数把工具再跑一遍，字符数对上才准进统计
（见 `economist/analyze.py` 的准入闸门）。那个办法对**本项目**有效，因为
这些工具是对本地语料的确定性只读。但它有一个致命局限，恰好落在本项目的
价值主张上：

    真实用户的工具来自**第三方 MCP server**。它们可能有副作用、可能非确定性、
    可能需要鉴权、可能读的是远端数据 —— **重放根本跑不起来。**

所以"重放"只能证明给自己看。要能给别人用，字段必须**在调用发生时原生记下来**。
这就是本模块存在的理由，也是它和 `attribution.py` 的分工：

    fields.py      ← 记录（调用发生时，不需要事后重放）
    attribution.py ← 判定口径（"被引用"到底怎么定义，唯一来源）

只留指纹，不留原文（两条独立的理由，都不是洁癖）
----------------------------------------------
1. **PII**：GenAI 约定用 `args_hash` 而不是参数原文，正是因为 trace 会流转到
   别人的可观测平台上。返回内容同理——记原文等于在自己的 trace 里种数据。
2. **体积**：把返回原文存进 trace，等于把"要省的那份上下文"又抄了一份存档。
   一个以省 token 为卖点的工具，自己先把 token 存了两遍，说不过去。

那"最小信息"是多少？判"被引用"只需要一件事：
**哪些值在这段返回里是新的、可区分的**（`attribution.is_distinctive`）。
所以记的就是这批值的小写形式，其余全部丢掉。

`before` 在**调用发生时**算，而不是事后重建
------------------------------------------
这是原生记录相对重放的第二个升级（第一个是"不用重放"）：调用发生时
`messages` 就在手边，"这个值在调用前是否已经在上下文里"**可以直接算**。
事后从 trace 重建 `before` 则是又一次重建——重建就要对账，对账就有准入闸门，
多一道就多一处可出错的地方。

代价必须说清楚：**`before` 被固化了。** 事后不能换个 `before` 口径重判
（重放路线可以）。这是刻意的交换：拿"可重判"换"不用重放"，因为后者才是
真实用户能用的那一条。
"""

from __future__ import annotations

import re

from findata.contextbudget import attribution

# 每段最多留多少个指纹的定义在 attribution 里（判定口径只有一个来源）
_MAX_FIELD_TOKENS = attribution.MAX_FIELD_TOKENS

# top-level 定义的起始行
_TOP_LEVEL_RE = re.compile(r"^(?:async\s+def|def|class)\s+(\w+)")
_FENCE_RE = re.compile(r"```[a-zA-Z]*\n(.*)\n```\Z", re.DOTALL)
_HEADING_RE = re.compile(r"^#{1,4} ", re.MULTILINE)


# --------------------------------------------------------------------------
# 切段（从 economist/analyze.py 下沉到这里：这是"工具返回的形状"，不是"经济学分析"）
# --------------------------------------------------------------------------


def extract_blocks(source: str) -> list[tuple[str, int, int, str]]:
    """切出 top-level 的 def/class 块。

    返回 [(名字, 起始行, 结束行, 原文)]。结束行 = 下一个 top-level 定义的前一行
    （或文件末）。这是"完整函数体"的来源，也是 `search_code` 大 payload 的来源。
    """
    lines = source.splitlines()
    starts: list[tuple[str, int]] = []
    for idx, line in enumerate(lines):
        match = _TOP_LEVEL_RE.match(line)
        if match:
            starts.append((match.group(1), idx))

    blocks: list[tuple[str, int, int, str]] = []
    for pos, (name, start) in enumerate(starts):
        end = starts[pos + 1][1] - 1 if pos + 1 < len(starts) else len(lines) - 1
        # 把紧贴在上面的装饰器与注释行并进来
        head = start
        while head > 0 and lines[head - 1].lstrip().startswith(("@", "#")):
            head -= 1
        text = "\n".join(lines[head : end + 1])
        blocks.append((name, head + 1, end + 1, text))
    return blocks


def segment_payload(
    tool: str, payload: str, *, result_code: str = "ok"
) -> list[attribution.FieldValue]:
    """把一次工具返回切成有意义的段。

    **非成功返回一律不切段。** 这条规则是实测撞出来的：错误/`not_found` 的返回是
    `No files under 'src/contextbudget/'.` 或
    `bad_arguments: unknown parameter(s) ['path: ']...` 这类**一句话**，
    按"每个非空行是一个路径"去切，会把这句话本身当成一个"路径值"，
    再被值级归因当真值用 —— 3B 臂上因此伪造出一条 `list_files → list_files`
    的信息流（样例值就是 `path:` 这个碎片）。
    判据很简单：**报了错的结果没有"结构"可言，不猜。**

    规模要如实说（**这里我自己先报错过一次**）：那一臂 `list_files` 共 13 次调用，
    5 次成功切出 **950 个真段 / 25,960 字符**，8 次非成功在旧规则下各切出 **1 个假段**
    （合计 8 段 / 864 字符）。所以 `958` 是"真段 + 假段"的**总数**，**不是**假段数——
    我一度把它写成了 958 个假段，等于把总量当成本次修复的收益，夸大了两个数量级。
    **教训：报"修掉了多少"之前，先确认这个数是"被修掉的部分"还是"总数"。**
    修掉假段的真实收益很小（8 段），这条规则的价值在于**它伪造的证据链**：
    一个不存在的 `list_files → list_files` 信息流会作为"最硬证据"被写进报告。

    **为什么不按 JSON 字段切**：本项目这批工具的返回是 Markdown 文本块，
    不是结构化 JSON。硬按"字段"切会切出没有语义的碎块，基于它的归因也就
    没有意义。真正有语义的边界是：`read_file` 的 top-level def/class 块、
    `search_code` 的每个命中块、`list_files` 的每个路径。

    **未知结构一律返回空表，不猜。** 空表在统计里读作"结构未观测"，
    绝不读作"没有内容所以可以删"——后者会让报告提议裁掉一个它根本没看懂的返回。
    """
    if not payload or result_code != "ok":
        return []

    if tool == "list_files":
        return [
            attribution.FieldValue(f"path:{line.strip()}", line.strip())
            for line in payload.splitlines()
            if line.strip()
        ]

    if tool == "read_file":
        body = _strip_fence(payload)
        if body is None:
            return []  # 非成功返回（"File not found" 之类）没有块结构
        blocks = extract_blocks(body)
        if blocks:
            return [
                attribution.FieldValue(f"block:{name}", text)
                for name, _s, _e, text in blocks
            ]
        return _heading_segments(body)

    if tool == "search_code":
        return _search_segments(payload)

    if tool == "get_signature":
        lines = [ln for ln in payload.splitlines() if ln.strip()]
        if not lines or not lines[0].startswith("# "):
            return []
        head = lines[0]
        out = [attribution.FieldValue(f"header:{head[:40]}", head)]
        out += [attribution.FieldValue(f"sig:{ln.strip()[:60]}", ln) for ln in lines[1:]]
        return out

    return []


def _strip_fence(payload: str) -> str | None:
    match = _FENCE_RE.fullmatch(payload)
    return match.group(1) if match else None


def _heading_segments(body: str) -> list[attribution.FieldValue]:
    """没有代码块的文件（如 .md）：按标题切；连标题都没有就整篇一段。"""
    parts = _HEADING_RE.split(body)
    headings = _HEADING_RE.findall(body)
    if not headings or len(parts) < 2:
        head = body.strip().splitlines()[0] if body.strip() else ""
        return [attribution.FieldValue(f"whole:{head[:40]}", body)] if body.strip() else []
    out: list[attribution.FieldValue] = []
    for idx, part in enumerate(parts[1:], start=1):
        title = part.splitlines()[0].strip() if part.strip() else f"section{idx}"
        out.append(attribution.FieldValue(f"section:{title[:40]}", part))
    return out


def _search_segments(payload: str) -> list[attribution.FieldValue]:
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
    out: list[attribution.FieldValue] = []
    if header and "".join(header).strip():
        out.append(
            attribution.FieldValue(f"header:{''.join(header).strip()[:40]}", "\n".join(header))
        )
    out += [attribution.FieldValue(f"hit:{name[:60]}", "\n".join(body)) for name, body in groups]
    return out


def segment_kind(name: str) -> str:
    """段名的前缀就是它的种类（`block:` / `hit:` / `path:` / `sig:` / `section:`）。"""
    return name.split(":", 1)[0] if ":" in name else "other"


# --------------------------------------------------------------------------
# 原生记录
# --------------------------------------------------------------------------
#
# `FieldRecord` 与 `make_record` 定义在 `attribution.py` 里 —— 因为
# "什么算可区分值""截断多少"是**判定口径**，不是记录格式。
# 本模块只负责"什么时候记、从哪段内容记"。


def record_fields(
    tool: str,
    payload: str,
    *,
    result_code: str = "ok",
    before_text: str = "",
) -> tuple[attribution.FieldRecord, ...]:
    """把一次工具返回记成字段记录表。

    `before_text` = 本次调用**之前**的上下文全文（系统提示 + 任务 + 历史）。
    **调用方必须传真实值**：传空串等于声明"上下文里什么都没有"，
    会让那些"值本来就在上下文里"的字段被误记成可区分值——那正是引用率虚高的老路。
    """
    segments = segment_payload(tool, payload, result_code=result_code)
    if not segments:
        return ()
    before_tokens = attribution.value_tokens(before_text)
    return tuple(
        attribution.make_record(
            fv.name,
            segment_kind(fv.name),
            fv.value,
            chars=fv.chars,
            before_tokens=before_tokens,
            max_tokens=_MAX_FIELD_TOKENS,
        )
        for fv in segments
    )


def record_fields_json(tool: str, payload: str, **kwargs: object) -> list[dict[str, object]]:
    """`record_fields` 的 JSON 形态（给落盘的 runs / spans 用）。"""
    records = record_fields(tool, payload, **kwargs)  # type: ignore[arg-type]
    return [r.to_dict() for r in records]
