"""C1 投资建议拒答 / C3 引用覆盖率 / C2 PII 脱敏 / D1 OCR 兜底。"""

from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest

from findata.finqa import ocr as ocr_mod
from findata.finqa.personal import (
    ADVICE_REFUSAL,
    answer_numbers,
    ask_free,
    citation_coverage,
    convert_file,
    is_advice_question,
    mask_pii_text,
)
from findata.finqa.qwen import CallRecord

# ── C1：决策建议型问题代码层拒答 ──


class TestIsAdviceQuestion:
    @pytest.mark.parametrize(
        "q",
        [
            "这只股票该不该买入？",
            "现在要不要卖出持仓？",
            "值不值得投资这个项目？",
            "能不能加仓？",
            "推荐一只股票给我",
            "你觉得它会涨吗",
            "目标价能到多少",
            "帮我选股",
        ],
    )
    def test_advice_questions_detected(self, q):
        assert is_advice_question(q), q

    @pytest.mark.parametrize(
        "q",
        [
            "该事件是否应当披露？",  # 客观合规问题，不是投资决策
            "违约金比例是多少？",
            "文档中的担保额度是多少？",
            "能不能查到等待期条款",  # 检索请求，非交易动作
            "营业收入 2024 年是多少",
        ],
    )
    def test_factual_questions_pass(self, q):
        assert not is_advice_question(q), q


class _RefuseIfCalled:
    """若被调用即失败——拒答必须发生在模型调用之前。"""

    def chat(self, qid, purpose, messages):  # pragma: no cover - 断言路径
        raise AssertionError("拒答问题不应触发模型调用")


class TestAskFreeRefusal:
    DOCS = {"doc1": "# d\n\n## Section 1\n\n借款金额 100 万元。\n"}

    def test_refusal_before_model_call(self):
        result = ask_free("该不该买入这只股票？", self.DOCS, _RefuseIfCalled())
        assert result["refused"] is True
        assert result["answer"] == ADVICE_REFUSAL
        assert result["total_tokens"] == 0
        assert "持牌投资顾问" in result["answer"]


class _FakeClient:
    def chat(self, qid, purpose, messages):
        reply = (
            "借款金额为 100 万元，期限 12 个月，利率 4.35%。"
            "依据【doc1 · Section 1】。"
        )
        return reply, CallRecord(
            qid=qid, purpose=purpose, model="fake",
            prompt_tokens=10, completion_tokens=5,
        )


class TestAskFreeCoverage:
    def test_coverage_fields_present(self):
        result = ask_free("借款金额是多少？", {
            "doc1": "# d\n\n## Section 1\n\n借款金额 100 万元，期限 12 个月，利率 4.35%。\n"
        }, _FakeClient())
        assert result["refused"] is False
        assert "coverage" in result and "uncovered_numbers" in result
        # 答案数字 100/12/4.35% 均在被引用片段里 → 覆盖率 1.0
        assert result["coverage"] == 1.0
        assert result["uncovered_numbers"] == []


# ── C3：覆盖率纯函数 ──


class TestAnswerNumbers:
    def test_filters_isolated_single_digit(self):
        assert answer_numbers("第1条约定第12条") == ["12"]

    def test_normalizes_thousands_separator(self):
        assert answer_numbers("金额 1,577.00 元") == ["1577.00"]

    def test_percent_and_year_kept(self):
        nums = answer_numbers("2024 年增长 30.5%")
        assert nums == ["2024", "30.5%"]

    def test_dedup(self):
        assert answer_numbers("100 元与 100 元") == ["100"]


class TestCitationCoverage:
    def test_full_coverage(self):
        cov, uncovered = citation_coverage(
            "营收 3,535.0 亿元", ["Net sales 3535.0 for 2022"]
        )
        assert cov == 1.0 and uncovered == []

    def test_partial_coverage_lists_numbers(self):
        cov, uncovered = citation_coverage(
            "营收 3,535.0，另据行业数据约 9,610", ["Net sales 3535.0"]
        )
        assert cov < 1.0
        assert uncovered == ["9610"]

    def test_no_numbers_full_coverage(self):
        assert citation_coverage("文档未提及", ["任何文本"]) == (1.0, [])


# ── C2：PII 脱敏 ──


class TestMaskPII:
    def test_id_card_masked(self):
        out, n = mask_pii_text("身份证号 11010119900307853X 备案")
        assert "11010119900307853X" not in out
        assert n == 1
        assert out.startswith("身份证号 110")  # 保留前 3

    def test_phone_masked(self):
        out, n = mask_pii_text("联系电话 13812345678。")
        assert "13812345678" not in out and n == 1

    def test_email_masked(self):
        out, n = mask_pii_text("邮箱 zhang.san@example.com 收")
        assert "zhang.san@example.com" not in out and n == 1

    def test_grouped_bank_card_masked(self):
        out, n = mask_pii_text("卡号 6222 0212 3456 7890 扣款")
        assert "6222 0212" not in out and n == 1

    def test_amounts_not_masked(self):
        """金额与常规数字串不误伤（金融问答的命根子）。"""
        text = "营业收入 1,234,567,890 元，账号 1234567890123，合同编号 20240011。"
        out, n = mask_pii_text(text)
        assert out == text and n == 0

    def test_convert_file_mask_option(self, tmp_path):
        src = tmp_path / "a.txt"
        src.write_text(
            "联系人手机 13812345678，借款 100 万元。\n", encoding="utf-8"
        )
        out_masked = convert_file(src, tmp_path, tmp_path / "c1", mask_pii=True)
        body = out_masked.read_text(encoding="utf-8")
        assert "13812345678" not in body
        assert "借款" in body
        assert src.read_text(encoding="utf-8").startswith("联系人手机 138")  # 原件不动


# ── D1：扫描页 OCR 兜底 ──


def _make_image_only_pdf(path: Path) -> None:
    """构造一页「只有图片、无文本」的 PDF（扫描件形态）。"""
    doc = pymupdf.open()
    page = doc.new_page()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 120, 60))
    page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()


class TestOCRFallback:
    def test_image_page_routes_to_ocr(self, tmp_path, monkeypatch):
        pdf = tmp_path / "scan.pdf"
        _make_image_only_pdf(pdf)
        monkeypatch.setattr(
            ocr_mod, "ocr_page_text", lambda page, dpi=200: "OCR 提取的文本"
        )
        # convert_pdf 函数内 import 拿的是模块当前属性，monkeypatch 生效
        from findata.finqa.personal import convert_pdf

        md = convert_pdf(pdf, "scan")
        assert "OCR 提取的文本" in md
        assert "## Page 1" in md

    def test_ocr_missing_returns_hint_not_silent(self, tmp_path, monkeypatch):
        pdf = tmp_path / "scan2.pdf"
        _make_image_only_pdf(pdf)
        monkeypatch.setattr(ocr_mod, "_OCR", None)
        monkeypatch.setattr(ocr_mod, "_OCR_TRIED", True)
        from findata.finqa.personal import convert_pdf

        md = convert_pdf(pdf, "scan2")
        assert "未安装 OCR 组件" in md  # 明示原因，绝不静默空文本


@pytest.mark.skipif(not ocr_mod.ocr_available(), reason="需要 extra=ocr")
class TestOCRReal:
    """真模型端到端：图片页渲染中文文本 → OCR 提取。extra 未装时跳过。"""

    def test_real_ocr_extracts_text(self, tmp_path):
        pdf = tmp_path / "scan_real.pdf"
        doc = pymupdf.open()
        page = doc.new_page()
        # 用系统字体渲染一行字，再把整页栅格化成图片替换原页（模拟扫描件）
        page.insert_text((72, 100), "LOAN 1000000", fontsize=24)
        pix = page.get_pixmap(dpi=200)
        doc2 = pymupdf.open()
        p2 = doc2.new_page()
        p2.insert_image(p2.rect, pixmap=pix)
        doc2.save(str(pdf))
        doc2.close()
        doc.close()
        from findata.finqa.personal import convert_pdf

        md = convert_pdf(pdf, "scan_real")
        assert "LOAN" in md or "1000000" in md or "OCR 失败" in md
