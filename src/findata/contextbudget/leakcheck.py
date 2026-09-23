"""语料级泄漏检查：**在一个语料里，答案卡在不在**。

为什么这必须是一个独立模块
--------------------------
`grading.has_exam_leak` 判的是**一条答案**有没有引用考题定义（答案侧）；
本模块判的是**一个语料目录**里有没有答案卡（语料侧）。两者是两条独立路径，
互相不能替代：

- 语料里没有答案卡 ⇒ 答案侧不可能泄漏（这条是充分的）。
- 语料里有答案卡 ⇒ 答案侧**可能**泄漏，也可能一次没读（2026-09-22 实测：
  `-fz` 批语料里答案卡在，但 128 个任务里只有 1 次尝试去读它，且路径写错了）。

为什么这条检查值得单独写一个模块（而不是在某脚本里写一行 `if path.exists()`）
--------------------------------------------------------------------------
2026-09-22 实测的一次事故：R5.0 的 32 道题、每题的 `must_contain` 判据、
和验收报告，**全都作为普通仓库文件留在语料快照里**。语料根就是本仓库的
冻结快照，于是"考题 + 判据 + 答案"与"待检索的代码"共处一个目录树。
后果不是"可能泄漏"，而是**确定泄漏**：

  同一条 `search_code("五档处置")`：
    含答案卡的语料 → 4,718 字符（把 `tasks.py` 里那道题的 `must_contain` 一起返回）
    不含答案卡的语料 → 22 字符（"No matches"）
  34 条真实查询里 **14 条**返回更大，每条多出约 +4,667 字符。

而按臂统计，这份泄漏是**偏向短清单臂的**：`active7b-fz` 有 5 个任务引用了考题
定义（其中 2 个本会被判"命中"），`all7b-fz` 只有 2 个、0 个假命中。
⇒ 于是"短清单质量不降反升"这条结论，在无泄漏语料上重算后**不成立**
（干净批 `exact_hit` 9 vs 7，k=2 < 6，无分辨力）。**一条结论就被这份泄漏翻掉了。**

两条独立判据，都要（存在同名事故：只查路径抓不到改名的副本）
----------------------------------------------------------
1. **路径判据**（`exam_card_in_corpus`）：受保护路径在不在。便宜、精确、可复现。
2. **内容判据**（`scan_corpus_for_exam_card`）：**按内容找**，与文件名无关。
   改个名的副本、被复制进别处的片段，只有这条能抓到。

判据 2 复用 `grading.LEAK_FINGERPRINTS` —— 那几个形状（`must_contain`、
`Task("t01", …`、`tags=("single"…`）**只会在定义处出现**，正常代码里不会
自然写成这样。故意取窄的理由与 `grading` 那边相同：一个"泄漏"标记会让人
去删数据，误标就是误删。

三条诚实边界
------------
- 判据 2 只能扫到**它见过的形状**。判据 2 没报 ≠ 没泄漏；判据 1 没报且判据 2
  没报，才能说"按现有指纹没找到"。**"未命中"不是"干净"。**
- 本模块只看**语料目录**，不看"模型有没有真的读它"。后者要靠
  `scripts/check_corpus_leak.py --replay-queries` 重放真实查询看返回大小。
- 二进制/超大文件不扫（按后缀白名单与大小上限），跳过数会报出来，
  **不静默丢**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .corpus import iter_corpus_files
from .grading import LEAK_FINGERPRINTS

__all__ = [
    "EXAM_CARD_RELPATHS",
    "ExamCardCheck",
    "check_exam_card",
    "exam_card_in_corpus",
    "scan_corpus_for_exam_card",
]

# 受保护的相对路径：**考题定义 + 判据 + 验收报告**。
# 这份清单是**声明**，不是推导 —— 所以它必须能被证伪：`check_exam_card` 会
# 逐条报告"在/不在/无法核对"，而不是只给一个布尔值。
#
# ⚠️ 这份清单**第一版是错的，而且是内容判据把它抓出来的**（2026-09-22 实测）：
# 我只写了 `tasks.py` / `grading.py` / 验收报告三条，跑内容判据发现
# `src/findata/contextbudget/__init__.py` 与 `tests/test_contextbudget.py`
# 里也含 `must_contain`（判据本体被 `from ... import` 和测试复制过去了）。
# ⇒ **凡是想只靠一份手写清单就下结论的地方，都会低估泄漏面。**
# 这两条是补进来的；下次新增判据文件时，先跑内容判据、再回头改这份清单。
EXAM_CARD_RELPATHS: tuple[str, ...] = (
    "src/findata/contextbudget/tasks.py",
    "src/findata/contextbudget/grading.py",
    "src/findata/contextbudget/__init__.py",
    "tests/test_contextbudget.py",
    "docs/r5.0-acceptance.md",
)

# 单文件扫描上限：超过就跳过并计数。设它是为了"跑得动"，不是为了"少报"。
_MAX_SCAN_BYTES = 2_000_000


@dataclass
class ExamCardCheck:
    """一个语料根的答案卡检查结果。

    `root` 为空 ⇒ 每项都是"无法核对"（`None`），**不是**"不在"。
    这是 `grading` 那边同一条纪律：**未知 ≠ 没有**。
    """

    root: str = ""
    present: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    unverifiable: bool = False
    content_hits: dict[str, list[str]] = field(default_factory=dict)
    n_files_scanned: int = 0
    n_files_skipped: int = 0

    @property
    def has_card(self) -> bool | None:
        """答案卡在不在。`None` = 无法核对。"""
        if self.unverifiable:
            return None
        return bool(self.present) or bool(self.content_hits)

    def describe(self) -> str:
        card = self.has_card
        if card is None:
            return "无法核对（账单里没记语料根）"
        if card:
            return (f"**在**（{len(self.present)} 个受保护路径 / "
                    f"{len(self.content_hits)} 个内容命中）")
        return "已移出（路径与内容两条判据都没命中）"


def exam_card_in_corpus(root: str | Path | None) -> bool | None:
    """**路径判据**：受保护路径在不在语料里。`None` = 无法核对。

    只回一个布尔值，供"必须快速决策"的地方用；要能解释清楚就用 `check_exam_card`。
    """
    if not root:
        return None
    base = Path(root)
    if not base.is_dir():
        return None
    return any((base / rel).exists() for rel in EXAM_CARD_RELPATHS)


def scan_corpus_for_exam_card(root: str | Path) -> tuple[dict[str, list[str]], int, int]:
    """**内容判据**：按内容找答案卡，与文件名无关。

    返回 `(命中的相对路径 → 命中的指纹原文, 扫了几个文件, 跳过了几个)`。

    跳过（读不了 / 太大）**会计数并报出**，不静默吞掉 —— 否则"扫了 0 个文件
    但没报泄漏"看起来和"干净"一样。
    """
    base = Path(root)
    hits: dict[str, list[str]] = {}
    n_scanned = n_skipped = 0
    for path in iter_corpus_files(base):
        try:
            if path.stat().st_size > _MAX_SCAN_BYTES:
                n_skipped += 1
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            n_skipped += 1
            continue
        n_scanned += 1
        found = [pat.pattern for pat in LEAK_FINGERPRINTS if pat.search(text)]
        if found:
            hits[path.relative_to(base).as_posix()] = found
    return hits, n_scanned, n_skipped


def check_exam_card(root: str | Path | None) -> ExamCardCheck:
    """两条判据一起跑，逐条报"在/不在/无法核对"。"""
    if not root:
        return ExamCardCheck(root="", unverifiable=True)
    base = Path(root)
    if not base.is_dir():
        return ExamCardCheck(root=str(root), unverifiable=True)

    chk = ExamCardCheck(root=str(base))
    for rel in EXAM_CARD_RELPATHS:
        (chk.present if (base / rel).exists() else chk.absent).append(rel)
    chk.content_hits, chk.n_files_scanned, chk.n_files_skipped = scan_corpus_for_exam_card(base)
    return chk
