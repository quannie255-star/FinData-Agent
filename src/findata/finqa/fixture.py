"""脚手架题集 / 文档的加载与校验（开发口径，官方数据未开放时使用）。

校验用 pydantic（成熟框架，不手写校验链）；fixture 目录约定：

    <root>/questions.json     # 题目数组，字段对齐赛题题面（qid/domain/split/
                              #   question/options/answer_format/type/doc_ids）
                              #   另加两个赛题没有的字段：gold（标准答案，开发用）
                              #   与 evidence（doc_id + clause，M4 证据层用）
    <root>/docs/<doc_id>.md   # 文档文本，首行必须是 `# <标题>`

加载即校验：字段缺失 / gold 不合法在 pydantic 层拦下；
doc_ids 悬空由 check_docs 拦——不许把坏 fixture 带进评分层。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, TypeAdapter, model_validator

from findata.finqa.scoring import normalize_answer

FORMAT_LABELS = {"mcq": "单选题", "multi": "多选题", "tf": "判断题"}


class Evidence(BaseModel):
    doc_id: str
    clause: str = ""


class Question(BaseModel):
    qid: str
    domain: str
    split: str = "A"
    question: str
    options: dict[str, str]
    answer_format: Literal["mcq", "multi", "tf"]
    type: str = ""
    # B 榜题目不给 doc_ids（官方口径），模型层允许为空；
    # 「A 榜 baseline 必须有 doc_ids」的约束在 baseline 层拦
    doc_ids: list[str] = []
    gold: str
    evidence: list[Evidence] = []

    @model_validator(mode="after")
    def _check_cross_fields(self) -> Question:
        # 选项数量按题型：mcq/multi 需 A–D，tf 需 A/B
        need = {"mcq": 4, "multi": 4, "tf": 2}[self.answer_format]
        required = {chr(ord("A") + i) for i in range(need)}
        if not set(self.options) >= required:
            raise ValueError(
                f"选项不足：需要 {sorted(required)}，现有 {sorted(self.options)}"
            )
        # gold 必须已是标准化形态：防止把 "acd" 这种松散写法带进判分
        gold = normalize_answer(self.gold, self.answer_format)
        if gold != self.gold or not gold:
            raise ValueError(f"gold 必须标准化且非空：{self.gold!r} → {gold!r}")
        if self.answer_format == "multi" and len(gold) < 2:
            raise ValueError("multi 题 gold 至少两个字母")
        return self

    def format_label(self) -> str:
        return FORMAT_LABELS[self.answer_format]


_question_list = TypeAdapter(list[Question])


def load_questions(root: Path) -> list[Question]:
    questions = _question_list.validate_json(
        (root / "questions.json").read_text(encoding="utf-8")
    )
    seen: set[str] = set()
    for q in questions:
        if q.qid in seen:
            raise ValueError(f"qid 重复：{q.qid}")
        seen.add(q.qid)
    return questions


def load_docs(root: Path, max_chars: int = 0) -> dict[str, str]:
    """读 docs/ 下全部 <doc_id>.md。max_chars>0 时截断（调费用），截断必须留痕。"""
    docs_dir = root / "docs"
    texts: dict[str, str] = {}
    for path in sorted(docs_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8").strip()
        if not text.startswith("# "):
            raise ValueError(f"{path.name}: 首行必须是 `# <标题>`")
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "\n\n（……以下内容因 max_chars 截断）"
        texts[path.stem] = text
    return texts


def check_docs(questions: list[Question], docs: dict[str, str]) -> None:
    """题集引用的每个 doc_id 都必须有对应文档，坏引用在这里失败。"""
    missing = sorted(
        {doc_id for q in questions for doc_id in q.doc_ids} - set(docs)
    )
    if missing:
        raise ValueError(f"题集引用了不存在的文档：{missing}")


def doc_title(text: str) -> str:
    """取文档首行标题（去掉 `# `），用于 prompt 里的文档头。"""
    return text.splitlines()[0].lstrip("# ").strip()
