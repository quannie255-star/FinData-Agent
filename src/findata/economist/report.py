"""渲染报告：数字 + 证据链 + 不确定性 + 不可迁移的部分。

**报告是这个工具唯一的产品形态**，所以它的措辞纪律比代码重要。四条：

1. **实测 / 换算 / 未测得，三者不许混排。** 每一组数字下面必须写清它是哪一种。
   本项目在 v3.0 上反复吃过这个亏：一个估算值放久了就被当成实测值引用。
2. **「未观测到」一律不许写成「没有」。** 段级归因的 `after` 里缺模型中间推理文本，
   所以「未观测到引用」这个判定的方向是**漏判**，报告里必须写在结论旁边。
3. **各提案的节省量不许简单相加。** 返回侧的提案可能作用于同一段字符
   （重复调用既进了「分层加载」也进了「结果复用」），相加等于重复计算。
   报告给分项 + 明确的重叠警告，不给一个「总共省 X%」的单一数字。
4. **不可迁移的部分原样写出来。** 本机 7B/3B 模型的结论里，token 侧可迁移
   （token 数由上下文结构决定），成功率侧只在本地口径下成立。

**排版纪律（写这个文件时踩过）**：字符串里嵌套引号一律用「」，不要用 ASCII 双引号
——它会把字面量切断，让整行变成 `STRING NAME STRING` 的语法错误。
"""

from __future__ import annotations

from findata.contextbudget import attribution
from findata.economist.analyze import Analysis, Verification

DISCLAIMER = (
    "本报告由 findata-context-economist 从 trace 自动生成。"
    "文中「实测」指 trace 里原样记录的 provider usage 或本项目数的确定性字符数；"
    "「换算」指由实测比值推出的估算值；「未测得」指本次输入不具备测它的条件。"
)


def _n(value: int | float) -> str:
    return f"{int(value):,}"


def _p(value: float) -> str:
    """p 值格式化：**不许打成 0.0000**。

    `p=0.0000` 读起来像"恰好为零"，而它实际是"小于四位小数能表示的量级"。
    写这个工具时实测踩到：符号检验 p≈2.6e-6 在 `:.4f` 下变成了 `0.0000`。
    精确的 0 在统计检验里几乎不可能出现，报成 0 会让人误以为算错了。
    """
    if value <= 0:
        return "p < 1e-12（下限）"
    if value < 1e-4:
        return f"p = {value:.2e}"
    return f"p = {value:.4f}"


def _usd(value: float) -> str:
    """金额按**量级**选格式。

    固定 `:.4f` 会把 $0.000041 打成 `$0.0000` —— 一个真实的、可累加的信号
    被格式化成"零"。写这个工具时实测踩到：4 位小数对"每个任务省几分钱"
    这个尺度根本不够。金额是估算值，不该用固定小数位假装精确。
    """
    if value == 0:
        return "$0"
    if abs(value) < 0.01:
        return f"${value:.3e}"
    return f"${value:.4f}"


def render(
    analysis: Analysis,
    *,
    verification: Verification | None = None,
    usd_per_mtok: float = 0.0,
    reproduce_cmd: str = "",
    generated_at: str = "",
) -> str:
    out: list[str] = []
    a = analysis

    out.append(f"# 上下文经济学报告 · {a.label}")
    out.append("")
    out.append(f"> {DISCLAIMER}")
    if generated_at:
        out.append(f"> 生成时间：{generated_at}")
    out.append("")

    out.extend(_render_inputs(a))
    out.extend(_render_three_numbers(a, verification=verification, usd_per_mtok=usd_per_mtok))
    out.extend(_render_tools(a))
    out.extend(_render_segments(a))
    out.extend(_render_flows(a))
    out.extend(_render_proposals(a))
    out.extend(_render_verification(verification, a))
    out.extend(_render_caveats(a))
    out.extend(_render_reproduce(reproduce_cmd))
    return "\n".join(out)


# --------------------------------------------------------------------------
# 1. 输入
# --------------------------------------------------------------------------


def _render_inputs(a: Analysis) -> list[str]:
    b = a.bundle
    out: list[str] = ["## 1. 本报告读的是什么", "", "| 项 | 值 |", "| --- | --- |"]
    out.append(f"| runs 增强层 | `{b.source_runs or '（未提供）'}` |")
    out.append(f"| OTel span 层 | `{b.source_spans or '（未提供）'}` |")
    out.append(f"| 任务数 | {len(b.runs)} |")
    out.append(f"| 工具调用 | {_n(b.n_tool_calls)} 次 |")
    out.append(f"| 模型调用轮数 | {_n(sum(r.n_turns for r in b.runs.values()))} |")
    out.append(f"| 模型 | {', '.join(b.models) or '（trace 未记录）'} |")
    out.append(f"| 语义约定 | `{b.semconv or '（trace 未记录）'}` |")
    out.append(f"| 工具清单识别 | {a.tool_list_match_note or '（未识别）'} |")
    out.append("")

    out.append("### 1.1 输入层的能力边界（来自数据层，不是报告作者补的）")
    out.append("")
    if b.gaps:
        out.extend(f"- {gap}" for gap in b.gaps)
    else:
        out.append("- 本次输入没有触发任何已声明的缺口")
    out.append("")

    r = a.reconstruct
    out.append("### 1.2 重建对账（字段级归因的准入闸门）")
    out.append("")
    if r.corpus_root:
        out.append(
            f"用 trace 里记录的参数重放工具，语料根 `{r.corpus_root}`。"
            f"共 {_n(r.n_calls)} 次调用：**{_n(r.n_ok)} 次重放字符数与记录值完全相等**"
            f"（覆盖率 {r.coverage:.1%}），{_n(r.n_mismatch)} 次不一致、"
            f"{_n(r.n_skipped_no_args)} 次参数不可解析、"
            f"{_n(r.n_skipped_no_root)} 次无语料根。"
        )
        out.append("")
        out.append(
            "**不一致的调用一律退出字段级统计**（不计分子也不计分母）——"
            "重建出来的内容与当时返回的内容不是同一份，基于它做的归因没有意义。"
        )
        if r.mismatches:
            out.append("")
            out.append("不一致样例：")
            out.append("")
            for row in r.mismatches[:5]:
                out.append(f"- `{row}`")
    else:
        out.append(
            "**未提供语料根 ⇒ 无法重建工具返回 ⇒ 字段级归因与段级统计全部未观测。**"
            "本报告只有工具级读数（调用次数、返回字符、结果码）。"
        )
    out.append("")

    # --- 字段级归因走的是哪条路（R5.1）。这个数字必须出现在报告里 ---
    # 理由：两条路**能力不同**。原生记录对第三方 MCP 工具也成立（不用重放），
    # 重放只对"只读、确定性、能拿到语料"的工具成立。混成一个数就把能力说大了。
    n_native, n_replayed = a.n_calls_native, a.n_calls_replayed
    out.append("### 1.3 字段级归因走的是哪条路（决定这套方法能给谁用）")
    out.append("")
    if n_native == 0 and n_replayed == 0:
        out.append("本批**没有任何调用拿到字段级判定**。")
    else:
        out.append(
            f"原生记录（调用发生时记下字段，**不需要重放**）：{_n(n_native)} 次调用；"
            f"事后重放：{_n(n_replayed)} 次调用。"
            + (
                f"另跳过 {_n(r.n_skipped_native)} 次**已带原生记录**的调用（不重放）。"
                if getattr(r, "n_skipped_native", 0)
                else ""
            )
        )
        out.append("")
        out.append(
            "**这两条路不能混报**：原生记录对第三方 MCP server 的工具同样成立"
            "（它们可能有副作用、非确定性、要鉴权，重放跑不起来）；"
            "重放只对本项目的确定性本地只读成立。只有原生那条路能给别人用。"
        )
    out.append("")
    return out


# --------------------------------------------------------------------------
# 2. 三组数字
# --------------------------------------------------------------------------


def _render_three_numbers(
    a: Analysis, *, verification: Verification | None, usd_per_mtok: float
) -> list[str]:
    out: list[str] = ["## 2. 三组数字", ""]
    c2t = a.chars_to_tokens
    ratio = c2t.ratio
    total_chars = sum(p.saving_chars for p in a.proposals)
    total_tokens = int(round(total_chars * ratio))

    out.append("### 2.1 换算基准（本批实测，不是查表常数）")
    out.append("")
    if c2t.n_turns:
        med = c2t.median
        out.append(
            f"从本批 {_n(c2t.n_turns)} 个模型调用实测："
            f"`prompt_tokens / (messages_chars + tools_chars)` "
            f"合计 {ratio:.4f}、中位 {med:.4f} token/字符"
            f"（p10–p90 = {c2t.p10:.4f}–{c2t.p90:.4f}）。"
        )
        out.append("")
        out.append(
            "分子是 provider usage（实测），分母是本项目数的字符（确定性）。"
            "**用这个系数系列算出来的 token 是估算值**；p10–p90 的宽度就是它不可信的幅度。"
        )
    else:
        out.append("**本批没有可用的 (tokens, chars) 配对 ⇒ 无法换算 token，只给字符口径。**")
    out.append("")

    out.append("### 2.2 省下的 token")
    out.append("")
    measured = _measured_delta(verification)
    if a.proposals and measured is not None:
        before_t, after_t, delta = measured
        pct = delta / before_t if before_t else 0.0
        out.append(
            f"- **实测**（配对任务 {verification.n_paired} 个，见 §7）："
            f"{_n(before_t)} → {_n(after_t)} prompt tokens，"
            f"**{delta:+,}（{pct:+.1%}）**"
        )
        out.append(
            f"- **换算（上界式估算，用于排序提案）**：各提案字符口径节省合计 "
            f"{_n(total_chars)} 字符 ⇒ 约 {_n(total_tokens)} prompt tokens"
            f"（按 2.1 的 {ratio:.4f} token/字符）"
        )
        out.append("")
        out.append(
            "**实测与估算不是一回事**：估算只覆盖 §6 里那几条提案，"
            "而实测差是两臂之间**全部**差异的结果。两者放在一起看的意义是标定"
            "「字符换算 token」这个做法的误差量级——见 §7.1。"
        )
    elif a.proposals:
        out.append("- **实测**：本次 trace 未做改后重跑 ⇒ 节省量**未实测**。")
        out.append(
            f"- **换算（上界式估算）**：各提案字符口径节省合计 {_n(total_chars)} 字符"
            f" ⇒ 约 {_n(total_tokens)} prompt tokens（按 2.1 的 {ratio:.4f} token/字符）。"
        )
    if a.proposals:
        out.append("")
        out.append(
            "这个估算是**上界**，两个方向的偏差同时存在，而且方向相反："
            "\n"
            "  - 偏高：段级「未观测到引用」受 `after` 缺中间推理文本影响，是**漏判** "
            "⇒ 被判为可省的段里混有实际被用到的内容；\n"
            "  - 偏低：只算了「工具定义」与「工具返回」两项，"
            "**没有**算历史消息重发与系统提示的优化空间。"
        )
        out.append("")
        out.append(
            "**各提案不可简单相加**：返回侧的两条提案可能作用于同一段字符"
            "（重复调用的返回既进「分层加载」也进「结果复用」）⇒ 相加会重复计算。"
            "分项数字见 §6，那里每条只算自己那一段。"
        )
        out.append("")
        out.append(
            "**还缺一侧代价（重要）**：返回侧的「分层加载」省的是 payload 字符，"
            "但模型拿不到全文后**可能要多调一次工具**（先取索引、再取正文）——"
            "这一侧的成本**没有计入**上面的节省量。真实净收益必须由改后重跑给出。"
        )
    else:
        out.append("**未生成任何提案 ⇒ 无可报的节省量**（不是 0，是没有被识别的机会）。")
    out.append("")

    out.append("### 2.3 折算成本")
    out.append("")
    if not a.proposals:
        out.append("无提案，不折算。")
    elif usd_per_mtok <= 0:
        out.append(
            "**未提供单价（`--usd-per-mtok`）⇒ 金额不折算。**"
            "刻意不给一个所谓「典型单价」的默认值：默认单价会让报告凭空长出一个"
            "看起来很专业的美元数字，而它对应的模型可能根本不是这个价。"
        )
    else:
        usd = total_tokens * usd_per_mtok / 1_000_000
        out.append(
            f"- 单价：**${usd_per_mtok} / 百万 prompt token**（由调用方提供，本工具不猜）"
        )
        out.append(
            f"- 换算：{_n(total_tokens)} tokens × ${usd_per_mtok}/M = "
            f"**{_usd(usd)} / 这 {len(a.bundle.runs)} 个任务**"
        )
        out.append("- 该金额继承 2.2 的全部偏差，只作量级参考")
    out.append("")

    out.append("### 2.4 成功率变化")
    out.append("")
    if verification is None or not verification.n_graded:
        out.append(
            "**未测得。** 要这个数，必须有**同一批任务**的改前/改后两臂 trace"
            "（`--after`），并且能给答案判分。本次输入不具备条件。"
        )
        out.append("")
        out.append(
            "这不是遗漏而是口径：把「没测」写成「没变化」是本项目在 v3.0 反复拆掉的那类错误。"
        )
    else:
        v = verification
        pct = (v.after_hits - v.before_hits) / v.n_graded if v.n_graded else 0.0
        out.append(f"- 判分器：`{v.grader}`（**弱判据**，只判关键词是否出现，判不出对错）")
        out.append(f"- 可判分任务：{v.n_graded} / {len(a.bundle.runs)}")
        out.append(f"- 关键词命中：改前 {v.before_hits} → 改后 {v.after_hits}")
        out.append(
            f"- 变化：**{pct:+.1%}**（只有改前命中 {v.only_before} 个 / "
            f"只有改后命中 {v.only_after} 个，McNemar 精确检验 {_p(v.mcnemar_p)}）"
        )
        out.append("")
        out.append(
            "`p` 大于 0.05 时只能说**没有证据表明变了**，"
            "不能反过来说**证明了没变**（样本量与判分器都撑不起那个结论）。"
        )
    out.append("")
    return out


# --------------------------------------------------------------------------
# 3 / 4 / 5
# --------------------------------------------------------------------------


def _render_tools(a: Analysis) -> list[str]:
    out: list[str] = ["## 3. 逐工具归因（实测）", ""]
    if not a.tool_aggs:
        out.append("本批没有工具调用。")
        out.append("")
        return out
    out.append("| 工具 | 调用 | 返回字符 | 结果码 | 有结构的调用 | 段数 | 段字符 |")
    out.append("| --- | ---: | ---: | --- | ---: | ---: | ---: |")
    for agg in sorted(a.tool_aggs.values(), key=lambda x: -x.payload_chars):
        codes = ",".join(f"{k}:{v}" for k, v in sorted(agg.codes.items()))
        out.append(
            f"| `{agg.tool}` | {agg.calls} | {_n(agg.payload_chars)} | {codes} | "
            f"{agg.n_calls_with_segments} | {agg.n_segments} | {_n(agg.seg_chars)} |"
        )
    out.append("")
    out.append(
        "「有结构的调用」= 返回被成功切成段的调用数。它小于「调用」的那些返回，"
        "本工具**不猜**其结构（多为 not_found / error 的短文本），因此不进段级统计。"
    )
    out.append("")
    return out


def _render_segments(a: Analysis) -> list[str]:
    out: list[str] = ["## 4. 段级归因（分档，不合成单一利用率）", ""]
    if not a.verdicts:
        out.append("段级归因未观测（没有语料根重建，或本批没有可切段的返回）。")
        out.append("")
        return out
    out.append(
        "| 工具 | 段种类 | 段数 | 段字符 | 被引用 | 未观测到引用 | 不可判 | "
        "被引用字符 | 未观测字符 |"
    )
    out.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    groups: dict[tuple[str, str], list] = {}
    for v in a.verdicts:
        groups.setdefault((v.tool, v.kind), []).append(v)
    for (tool, kind), group in sorted(
        groups.items(), key=lambda kv: -sum(v.chars for v in kv[1])
    ):
        n_ref = sum(1 for v in group if v.status == attribution.STATUS_REFERENCED)
        n_not = sum(1 for v in group if v.status == attribution.STATUS_NOT_REFERENCED)
        n_unv = sum(1 for v in group if v.status == attribution.STATUS_UNVERIFIABLE)
        c_ref = sum(v.chars for v in group if v.status == attribution.STATUS_REFERENCED)
        c_not = sum(v.chars for v in group if v.status == attribution.STATUS_NOT_REFERENCED)
        out.append(
            f"| `{tool}` | {kind} | {len(group)} | {_n(sum(v.chars for v in group))} | "
            f"{n_ref} | {n_not} | {n_unv} | {_n(c_ref)} | {_n(c_not)} |"
        )
    out.append("")
    out.append("读法（三档含义不同，不可合并）：")
    out.append("")
    out.append("- **被引用**：值在调用之后出现在后续文本里，且此前不在上下文里。")
    out.append(
        "- **未观测到引用**：本次运行没看到引用。**不等于该段无用**——"
        "可能只是这批任务没覆盖；且 `after` 缺中间推理文本，这个方向是**漏判**。"
    )
    out.append(
        "- **不可判**：值太短或太常见（撞车概率高），或可区分值本来就在上下文里。"
        "**既不计分子也不计分母**。"
    )
    out.append("")
    out.append(
        "「被引用字符」天然偏高：大段只要命中一个值就整段计入。"
        "合法用法是配着段数与命中比例一起看，不能单独引用。"
    )
    out.append("")

    thin = sorted(
        (v for v in a.verdicts if v.status == attribution.STATUS_REFERENCED and v.chars >= 1500),
        key=lambda v: -(v.chars / max(1, v.n_matched)),
    )[:5]
    if thin:
        out.append("### 4.1 证据链样例：被引用但命中很稀的大段")
        out.append("")
        out.append("| 任务 | 工具 | 段 | 段字符 | 命中/可判 | 命中的值（样例） |")
        out.append("| --- | --- | --- | ---: | ---: | --- |")
        for v in thin:
            sample = "、".join(f"`{t}`" for t in (v.as_arg + v.in_answer)[:3])
            out.append(
                f"| {v.task[:18]} | `{v.tool}` | `{v.name[:28]}` | {_n(v.chars)} | "
                f"{v.n_matched}/{v.n_candidates} | {sample} |"
            )
        out.append("")
        out.append(
            "这些段被判为「被引用」，但整段 1500+ 字符里只有个位数个值出现了。"
            "**这不构成「整个返回都被用上」的证据**——它恰好是分层加载的候选。"
        )
        out.append("")
    return out


def _render_flows(a: Analysis) -> list[str]:
    out: list[str] = ["## 5. 跨工具信息流（本工具里最硬的证据）", ""]
    if not a.flows:
        out.append("未观测到跨工具信息流：没有任何工具返回的值出现在**后续工具调用的参数**里。")
        out.append("")
        out.append(
            "边界：「值进了最终答复但没再调工具」不在本表里（见 §4）。"
        )
        out.append("")
        return out
    out.append(
        "每一行都可核对：A 返回的某个值，出现在**后续调用** B 的参数里。"
        "参数由 trace 原样记录，不存在「散文里提到算不算引用」的解释空间。"
    )
    out.append("")
    out.append("| 从 | 到 | 命中实例 | 涉及段 | 样例值 |")
    out.append("| --- | --- | ---: | ---: | --- |")
    for flow in sorted(a.flows.values(), key=lambda f: -f.n_token_hits):
        samples = "、".join(f"`{s}`" for s in flow.samples[:5])
        out.append(
            f"| `{flow.from_tool}` | `{flow.to_tool}` | {flow.n_token_hits} | "
            f"{flow.n_segments} | {samples} |"
        )
    out.append("")
    out.append(
        "「命中实例」= 命中次数（同一个值被多次用到会多次计数），"
        "**不是去重后的不同值个数**；「涉及段」= 该方向有多少个返回段贡献过值。"
    )
    out.append("")
    return out


# --------------------------------------------------------------------------
# 6 / 7 / 8 / 9
# --------------------------------------------------------------------------


def _render_proposals(a: Analysis) -> list[str]:
    out: list[str] = ["## 6. 改动提案（可执行 + 可逆 + 带代价）", ""]
    if not a.proposals:
        out.append("未生成提案。可能原因：未识别工具清单、无可重建的返回、或本批没有可省的量。")
        out.append("")
        return out
    ratio = a.chars_to_tokens.ratio
    for idx, p in enumerate(a.proposals, start=1):
        out.append(f"### 6.{idx} {p.lever}")
        out.append("")
        out.append(f"- **对象**：{p.target}")
        out.append(f"- **动作**：{p.action}")
        out.append(f"- **证据**：{p.evidence}")
        out.append(f"- **证据强度**：{p.strength}")
        out.append(f"- **可逆**：{'是' if p.reversible else '否'}")
        out.append(f"- **影响调用数**：{p.affected_calls}")
        out.append(
            f"- **代价（字符口径，实测）**：{_n(p.saving_chars)} 字符"
            f" ⇒ 约 {_n(int(round(p.saving_chars * ratio)))} tokens（换算）"
        )
        out.append("")
    out.append(
        "**处置纪律**：所有提案都是**可逆**的（保留实现、内容仍可取回），"
        "没有一条是删除信息。依据是项目一贯的判据——证据弱时选可逆的动作，"
        "别让一个「我没观测到」的判断去执行有代价且不可逆的操作。"
    )
    out.append("")
    return out


def _render_verification(v: Verification | None, a: Analysis) -> list[str]:
    out: list[str] = ["## 7. 改前/改后对照（验证环节）", ""]
    if v is None:
        out.append("本次未提供改后一臂 ⇒ **对照未做**。")
        out.append("")
        return out
    out.append(
        f"配对任务：**{v.n_paired}** 个"
        f"（改前独有 {v.n_unpaired_before}，改后独有 {v.n_unpaired_after}）"
    )
    out.append("")
    out.append("| 量 | 改前 | 改后 | 差 |")
    out.append("| --- | ---: | ---: | ---: |")
    out.append(
        f"| prompt tokens（逐轮求和，实测） | {_n(v.before_tokens)} | {_n(v.after_tokens)} | "
        f"{v.after_tokens - v.before_tokens:+,} |"
    )
    out.append(
        f"| 工具调用次数 | {_n(v.before_calls)} | {_n(v.after_calls)} | "
        f"{v.after_calls - v.before_calls:+,} |"
    )
    out.append(
        f"| 工具返回字符 | {_n(v.before_payload)} | {_n(v.after_payload)} | "
        f"{v.after_payload - v.before_payload:+,} |"
    )
    out.append("")
    if v.paired_diffs:
        out.append(
            f"逐任务差值：更省的 {v.n_neg} 个 / 更贵的 {v.n_pos} 个 / 持平 {v.n_equal} 个，"
            f"中位差 {_median(v.paired_diffs):+,} tokens，符号检验 {_p(v.sign_p)}。"
        )
        out.append("")
        out.append("用符号检验而不是 t 检验：token 差值明显长尾，t 检验会高估显著性。")
        out.append("")
    out.extend(_estimate_vs_measured(v, a))
    if v.notes:
        out.extend(f"- {note}" for note in v.notes)
        out.append("")
    return out


def _estimate_vs_measured(v: Verification, a: Analysis) -> list[str]:
    """把 §6 的定义侧估算与本节的实测差放在一起 —— 这是估算值唯一该被信任的用法。

    只对比**定义侧**那一条：本次改的就是工具清单，返回侧没动，
    拿全量估算去比实测差会把没改的东西也算进来，然后得出「估算不准」的假结论。
    """
    lever = next((p for p in a.proposals if p.lever.startswith("定义侧")), None)
    if lever is None:
        return []
    est = int(round(lever.saving_chars * a.chars_to_tokens.ratio))
    measured = v.before_tokens - v.after_tokens
    out = [
        "### 7.1 估算 vs 实测（定义侧那一条）",
        "",
        f"- §6 的**字符口径估算**：约 {_n(est)} tokens（换算）",
        f"- 本节**实测**差：{_n(measured)} tokens（改前 − 改后）",
    ]
    if measured > 0:
        ratio = est / measured
        out.append(
            f"- 估算/实测 = **{ratio:.2f}×**"
            f"（{'偏保守' if ratio < 1 else '偏高'}）"
        )
        out.append("")
        out.append(
            "这个倍数就是「用字符换算 token」这个做法的误差量级。"
            "它值得被记住：估算的用途是**排序提案**，不是替代重跑。"
        )
    else:
        out.append(
            "- 实测差不大于 0 ⇒ 这次改动**没有省到 token**。"
            "此时估算值与实测值不是「准不准」的关系，而是估算漏掉了真实的代价"
            "（例如模型多调了几次工具），必须按实测来报。"
        )
    out.append("")
    return out


def _render_caveats(a: Analysis) -> list[str]:
    out: list[str] = ["## 8. 不确定性 / 未观测 / 不可迁移", ""]
    out.append("### 8.1 本次**未观测**的量（不是 0，是没测）")
    out.append("")
    out.extend(f"- {item}" for item in unobserved(a))
    out.append("")
    out.append("### 8.2 不可迁移的部分")
    out.append("")
    out.append(
        "- **token 侧结论可迁移**：prompt token 数由上下文结构决定，"
        "换成更大的模型，被裁掉的那段字符仍然不会进上下文。"
    )
    out.append(
        "- **成功率侧只在本地口径下成立**：本批用的是本机小模型，"
        "更大模型对同一处上下文裁剪的敏感度**可能不同**"
        "（可能更不敏感，也可能因为少了关键线索而更早走偏）。要迁移必须先重跑。"
    )
    out.append(
        "- **段切分规则是跟着这批工具写的**：`read_file` 按 def/class 块切，"
        "前提是返回为 Markdown 代码块。换成结构化 JSON 返回的工具，"
        "切分要换实现，结论不能照搬。"
    )
    out.append("")
    out.append("### 8.3 方法本身的已知盲区")
    out.append("")
    out.append(
        "- 段级归因只扫 **ASCII 标识符与数字**；中文散文里的引用判不了，"
        "这类一律进「不可判」档。"
    )
    out.append("- **字段名 ≠ 字段值**：模型提到字段名而没用它的值，不计为引用。")
    out.append(
        "- 大段只要命中一个值就整段计入「被引用」⇒「被引用字符」天然偏高。"
    )
    out.append(
        "- 同一轮内的兄弟调用互不进对方的 `before`/`after`（模型是同一条消息发的），"
        "所以同轮并行调用之间的信息流**未观测**。"
    )
    out.append(
        "- 本工具**不产出单一的「节省百分比」**：段级「未观测到引用」是弱证据，"
        "合成一个百分比会让弱证据看起来像结论。分档与分项才是可被反驳的形态。"
    )
    out.append("")
    return out


def _render_reproduce(reproduce_cmd: str) -> list[str]:
    out: list[str] = ["## 9. 复现", ""]
    if reproduce_cmd:
        out.append("```bash")
        out.append(reproduce_cmd)
        out.append("```")
        out.append("")
    out.append(
        "本工具的输出是**确定性**的（除时间戳）：输入 trace 不变，报告的每个数字都不变。"
        "唯一的不确定性来自 `--usd-per-mtok`（调用方给）与 2.1 的换算系数（本批实测）。"
    )
    out.append("")
    return out


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _measured_delta(v: Verification | None) -> tuple[int, int, int] | None:
    """实测的 (改前, 改后, 差)。没有可用对照时返回 `None`（**不是 0**）。"""
    if v is None or not v.n_paired:
        return None
    return v.before_tokens, v.after_tokens, v.after_tokens - v.before_tokens


def unobserved(a: Analysis) -> list[str]:
    items: list[str] = []
    if not a.bundle.has_runs:
        items.append("逐任务字段级归因（缺 runs 增强层）")
    if not a.reconstruct.corpus_root:
        items.append("工具返回内容与字段级归因（未提供语料根，无法重建）")
    if a.reconstruct.n_mismatch:
        items.append(
            f"{a.reconstruct.n_mismatch} 次调用的重建与记录不一致"
            "（语料变了或工具非确定性），这些调用退出字段级统计"
        )
    items.append("改后重跑的成功率（除非 §7 给出了对照）")
    items.append("重试造成的浪费（本 loop 不重试，`retry_count` 恒为 0 ⇒ 未观测）")
    if a.bundle.has_run_level_parent_span is False:
        items.append("span 之间的任务归属（被观测系统缺 run 级父 span，见 §1.1）")
    return items


def render_json(
    analysis: Analysis,
    *,
    verification: Verification | None = None,
    usd_per_mtok: float = 0.0,
) -> dict:
    """机器可读版本：供 CI 断言或用别的前端渲染。"""
    a = analysis
    ratio = a.chars_to_tokens.ratio
    total_chars = sum(p.saving_chars for p in a.proposals)
    # tokens 先取整，再拿**取整后的那个数**去折算金额：
    # 否则读报告的人用 saving_tokens_est × 单价 算出来的数与 usd_est 对不上，
    # 一个可以被独立复核的数字链就断了。
    tokens_est = int(round(total_chars * ratio))
    return {
        "label": a.label,
        "inputs": {
            "runs": a.bundle.source_runs,
            "spans": a.bundle.source_spans,
            "n_tasks": len(a.bundle.runs),
            "n_tool_calls": a.bundle.n_tool_calls,
            "n_tool_spans": len(a.bundle.tool_spans),
            "n_distinct_trace_ids": a.bundle.n_distinct_trace_ids,
            "models": list(a.bundle.models),
            "semconv": a.bundle.semconv,
            "gaps": a.bundle.gaps,
            "tool_list_match": a.tool_list_match_note,
        },
        "reconstruct": a.reconstruct.to_dict(),
        "chars_to_tokens": a.chars_to_tokens.to_dict(),
        "tools": [
            {
                "tool": agg.tool,
                "calls": agg.calls,
                "payload_chars": agg.payload_chars,
                "codes": dict(agg.codes),
                "n_segments": agg.n_segments,
                "seg_chars": agg.seg_chars,
                "n_referenced": agg.n_referenced,
                "n_not_referenced": agg.n_not_referenced,
                "n_unverifiable": agg.n_unverifiable,
                "n_calls_with_segments": agg.n_calls_with_segments,
                "n_calls_no_shape": agg.n_calls_no_shape,
            }
            for agg in sorted(a.tool_aggs.values(), key=lambda x: -x.payload_chars)
        ],
        "flows": [
            {
                "from": f.from_tool,
                "to": f.to_tool,
                "n_token_hits": f.n_token_hits,
                "n_segments": f.n_segments,
                "samples": f.samples,
            }
            for f in sorted(a.flows.values(), key=lambda x: -x.n_token_hits)
        ],
        "proposals": [p.to_dict(ratio) for p in a.proposals],
        "saving_chars_upper_bound": total_chars,
        "saving_tokens_est": tokens_est,
        "usd_per_mtok": usd_per_mtok,
        # **不四舍五入**：金额可能只有 1e-5 量级，round(..., 4) 会把它变成 0.0，
        # 那等于把一个真实信号格式化没了。格式化交给渲染层按量级选精度（见 _usd）。
        "usd_est": (tokens_est * usd_per_mtok / 1e6 if usd_per_mtok > 0 else None),
        "verification": verification.to_dict() if verification else None,
        "unobserved": unobserved(a),
    }
