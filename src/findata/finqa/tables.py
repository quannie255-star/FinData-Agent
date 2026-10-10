"""表格保真：HTML 剥平语料的表格行重组 + PDF 表格还原的共享渲染。

背景（FinanceBench 校准第一期定位的失败模式）：EDGAR 抓取的 `_strip_html`
把 ``<td>/<tr>`` 全部替换成空格，表格变成「每单元格一行、行间空行」的
碎片流——词袋检索分不受影响，但：① 标签与数值的行邻接丢失，二级切分后
可能分家；② 喂给模型的上下文不可读，作答阶段拿不到行结构。

本模块做两件事：
- ``reassemble_flattened(text)``：对既有「单元格一行」语料做确定性重组——
  连续的短单元格行合并为 ``a | b | c`` 行（行边界未知，按窗口组合；
  词袋口径不变，换来行邻接与可读性）；
- ``render_markdown_table(rows)``：PDF 表格检测（PyMuPDF find_tables）与
  OCR 路线共用的 Markdown 管道表渲染。

确定性纪律：无模型、无随机；同样输入永远同样输出。
"""

from __future__ import annotations

import re

# 单元格行：短（≤60 字符）、以字母/数字/货币/百分号为主体、不以句号收尾
# （句号收尾的是散文句，不是单元格）
_CELL_LINE = re.compile(r"^[\sA-Za-z0-9$_(),.%:/×\-—–&;'\"]{1,60}$")
_HAS_ALNUM = re.compile(r"[A-Za-z0-9]")
_SENTENCE_END = re.compile(r"[.。;；]\s*$")

# 一次重组进一个行线的单元格数：太小则行数爆炸、太大则单行超长被二次切分
_CELLS_PER_ROW = 8
# 触发表格判定的最少连续单元格数（低于此视为零散短行，不动）
_MIN_RUN = 4


def _is_cell(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 60:
        return False
    if not _HAS_ALNUM.search(stripped):
        return False
    # 散文句（含句末标点）不进表格；表格单元格偶尔含句点（"$1.5"）但极少收尾句号
    if _SENTENCE_END.search(stripped):
        return False
    return bool(_CELL_LINE.match(stripped))


def reassemble_flattened(text: str) -> str:
    """把「单元格一行 + 空行」的剥平表格重组为管道行。

    只动满足表格签名（≥ ``_MIN_RUN`` 个连续单元格行，中间可夹空行）的区段；
    散文、中文条款原文一律原样保留。重组后标签与数值进入同一行线，
    行内绝不被二级切分腰斩（retrieval._split_long 只按行边界切）。
    """
    lines = text.splitlines()
    out: list[str] = []
    run: list[str] = []

    def flush(restore_blank_after: bool = False) -> None:
        if len(run) >= _MIN_RUN:
            for i in range(0, len(run), _CELLS_PER_ROW):
                row = " | ".join(run[i : i + _CELLS_PER_ROW])
                out.append(f"| {row} |")
            out.append("")  # 表块与后续文本之间留空行
        elif run:
            # 未达表格签名：还原原形态（单元格行之间与其后被吞的空行一并还原）
            out.extend("\n\n".join(run).split("\n"))
            if restore_blank_after:
                out.append("")
        run.clear()

    for line in lines:
        if _is_cell(line):
            run.append(line.strip())
            continue
        if line.strip() == "" and run:
            continue  # 单元格行之间的空行：还在表格里，吞掉
        flush(restore_blank_after=bool(run))
        out.append(line)
    flush()
    # 去掉吞空行可能造成的连续 3+ 空行；保留原文的尾换行形态
    out_text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    if text.endswith("\n") and not out_text.endswith("\n"):
        out_text += "\n"
    return out_text


def render_markdown_table(rows: list[list[str]]) -> str:
    """行列二维数据 → Markdown 管道表（首行为表头）。

    单元格内的管道符替换为全角｜防串列；空表返回空串。
    """
    if not rows:
        return ""
    n_cols = max(len(r) for r in rows)
    safe_rows = [ [_sanitize(c) for c in r] + [""] * (n_cols - len(r)) for r in rows ]
    head = safe_rows[0]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * n_cols]
    for r in safe_rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def _sanitize(cell: str) -> str:
    return " ".join(str(cell).split()).replace("|", "｜")
