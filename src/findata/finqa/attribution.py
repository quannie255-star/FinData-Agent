"""M4 引用观测归因：从判定/作答回复的实际引用，反推证据的真实使用率。

v4.0 方法论的迁移：压缩什么不由启发式决定，由「模型实际用了什么」决定。
两条观测线：

- isolate 臂：片段级使用率（喂 k 个片段，判定实际引用几个、是第几位）；
- memory 臂：记忆行级使用率（事实清单哪些行被引用、哪些白压了）。

诚实规则：**引用只有匹配到该次调用上下文里真实存在的内容才算使用**——
判定回复可能引用不在场条款（复述常识或幻觉），那种引用不产生使用记录，
也不许进统计（否则使用率虚高，压缩决策就会被带偏）。
"""

from __future__ import annotations

import re

_BRACKET = re.compile(r"【([^【】]+)】")
_ARTICLE = re.compile(r"第[一二三四五六七八九十百千]+条")
_NUMBERED = re.compile(r"\d+\.\d+")


def extract_citations(reply: str) -> list[str]:
    """抽取回复里的引用串：括号头、裸条款号、编号节。

    返回原始引用串（不去重、不判在场——匹配是 match_citations 的职责，
    抽取层保持无判断）。
    """
    citations: list[str] = []
    citations.extend(_BRACKET.findall(reply))
    # 括号内容里可能还含条款号（【strict_csrc_035 · 第四十七条】），
    # 裸条款号单独抽会重复——先抽括号外的
    stripped = _BRACKET.sub(" ", reply)
    citations.extend(_ARTICLE.findall(stripped))
    citations.extend(_NUMBERED.findall(stripped))
    return citations


def _normalize_header(header: str) -> str:
    return header.replace(" ", "").replace("　", "")


def _citation_matches(citation: str, doc_id: str, header: str) -> bool:
    """单条引用是否指向 (doc_id, header) 这个在场单元。"""
    cite = _normalize_header(citation)
    h = _normalize_header(header)
    if "·" in cite:
        left, _, right = cite.partition("·")
        return _normalize_header(doc_id) in left and right and (
            right == h or h.startswith(right) or right.startswith(h)
        )
    # 裸条款号 / 编号节：只认 header 对得上（跨文档同名条款记到所有在场同号
    # 片段——判定没有义务报文档号，宁可多记不可漏记，统计口径写明）
    return bool(cite) and (h == cite or h.startswith(cite) or cite.startswith(h))


def match_citations(
    citations: list[str], units: list[tuple[str, str]]
) -> set[int]:
    """把引用匹配到在场单元（doc_id, header），返回命中的下标集合。

    只匹配在场内容：引用了不在场的条款不产生任何记录。
    """
    used: set[int] = set()
    for citation in citations:
        for i, (doc_id, header) in enumerate(units):
            if _citation_matches(citation, doc_id, header):
                used.add(i)
    return used


def usage_stats(
    records: list[dict[str, object]],
) -> dict[str, object]:
    """汇总使用率读数。

    records 每项：{"qid", "fed": [(doc_id, header, chars)], "used_idx": set}。
    输出：整体使用率、按喂入位次的使用率、浪费字符量与占比。
    """
    fed_total = used_total = 0
    by_rank: dict[int, list[int]] = {}
    wasted_chars = 0
    fed_chars = 0
    for record in records:
        fed = record["fed"]
        used_idx = record["used_idx"]
        fed_total += len(fed)
        used_total += len(used_idx)
        for rank, (_doc, _header, chars) in enumerate(fed, start=1):
            slot = by_rank.setdefault(rank, [0, 0])
            slot[1] += 1
            fed_chars += chars
            if rank - 1 in used_idx:
                slot[0] += 1
            else:
                wasted_chars += chars
    rank_rates = {
        rank: {"used": used, "fed": fed, "rate": round(used / fed, 4) if fed else 0.0}
        for rank, (used, fed) in sorted(by_rank.items())
    }
    return {
        "fed_units": fed_total,
        "used_units": used_total,
        "usage_rate": round(used_total / fed_total, 4) if fed_total else 0.0,
        "by_rank": rank_rates,
        "fed_chars": fed_chars,
        "wasted_chars": wasted_chars,
        "wasted_ratio": round(wasted_chars / fed_chars, 4) if fed_chars else 0.0,
    }


def stats_lines(stats: dict[str, object]) -> list[str]:
    """报告里的归因读数段（成本如实：浪费即成本）。"""
    lines = [
        f"证据使用率：{stats['used_units']}/{stats['fed_units']}"
        f"（{stats['usage_rate']:.2%}）",
        f"浪费字符：{stats['wasted_chars']:,} / {stats['fed_chars']:,}"
        f"（{stats['wasted_ratio']:.2%}）",
        "按喂入位次：",
    ]
    for rank, slot in stats["by_rank"].items():
        lines.append(
            f"  第 {rank} 位：{slot['used']}/{slot['fed']}（{slot['rate']:.0%}）"
        )
    return lines
