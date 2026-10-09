"""个人文档库与自由问答：转换往返、端点行为、引用归因（零真实网络）。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from findata.finqa import service
from findata.finqa.personal import ask_free, convert_file, safe_stem
from findata.finqa.qwen import CallRecord


class TestSafeStem:
    def test_keeps_cjk_alnum(self):
        assert safe_stem("比亚迪年报2025.pdf") == "比亚迪年报2025"

    def test_replaces_specials(self):
        out = safe_stem("my report (final).pdf")
        assert out == "my_report_final"
        assert not set(out) & set(" ().")

    def test_empty_falls_back(self):
        assert safe_stem("$$$..pdf") == "document"


class TestConvertFile:
    def test_txt_without_headers_gets_sections(self, tmp_path):
        src = tmp_path / "documents" / "note.txt"
        src.parent.mkdir(parents=True)
        src.write_text("第一段内容。\n" * 300, encoding="utf-8")
        out = convert_file(src, src.parent, tmp_path / "converted")
        text = out.read_text(encoding="utf-8")
        assert text.startswith("# note")
        assert "## Section 1" in text

    def test_md_keeps_existing_headers(self, tmp_path):
        src = tmp_path / "documents" / "book.md"
        src.parent.mkdir(parents=True)
        src.write_text("# 书\n\n## 第一章\n内容", encoding="utf-8")
        out = convert_file(src, src.parent, tmp_path / "converted")
        assert "## 第一章" in out.read_text(encoding="utf-8")

    def test_pdf_roundtrip(self, tmp_path):
        import pymupdf

        src = tmp_path / "documents" / "tiny.pdf"
        src.parent.mkdir(parents=True)
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), "The guarantee fee is 0.5 percent per year.")
        doc.save(src)
        out = convert_file(src, src.parent, tmp_path / "converted")
        assert "## Page 1" in out.read_text(encoding="utf-8")

    def test_docx_roundtrip(self, tmp_path):
        import docx

        src = tmp_path / "documents" / "report.docx"
        src.parent.mkdir(parents=True)
        d = docx.Document()
        d.add_paragraph("公司营业收入为 803,964,958,000.00 元。")
        d.save(src)
        out = convert_file(src, src.parent, tmp_path / "converted")
        assert "803,964,958,000.00" in out.read_text(encoding="utf-8")

    def test_unsupported_rejected(self, tmp_path):
        src = tmp_path / "documents" / "x.exe"
        src.parent.mkdir(parents=True)
        src.write_bytes(b"MZ")
        with pytest.raises(ValueError, match="不支持的格式"):
            convert_file(src, src.parent, tmp_path / "converted")


class _FakeClient:
    def chat(self, qid, purpose, messages):
        reply = (
            "公司营业收入为 803,964,958,000.00 元"
            "【report · Section 1】。"
        )
        return reply, CallRecord(
            qid=qid, purpose=purpose, model="fake",
            prompt_tokens=100, completion_tokens=20,
        )


class TestAskFree:
    def test_answer_with_verified_citations(self):
        docs = {
            "report": (
                "# report\n\n## Section 1\n\n公司营业收入为 803,964,958,000.00 元。"
            )
        }
        result = ask_free("公司营业收入是多少", docs, _FakeClient())
        assert "803,964,958,000.00" in result["answer"]
        assert result["citations"] == [{"doc_id": "report", "section": "Section 1"}]
        assert result["total_tokens"] == 120

    def test_citation_not_in_context_not_recorded(self):
        docs = {"report": "# report\n\n## Section 1\n\n内容"}
        client = _FakeClient()
        # 模型引用了不在场的「幻影章节」——归因规则不计入
        client.chat = lambda qid, purpose, messages: (
            "引用【report · 不存在的章节】。",
            CallRecord(qid=qid, purpose=purpose, model="fake",
                       prompt_tokens=10, completion_tokens=2),
        )
        result = ask_free("问题", docs, client)
        assert result["citations"] == []


class TestLibraryEndpoints:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        service.USER_DOCS_DIR = tmp_path / "documents"
        service.USER_CONVERTED_DIR = tmp_path / "converted"
        monkeypatch.setattr(service, "build_client", _FakeClient)
        return TestClient(service.app)

    def test_upload_list_delete_roundtrip(self, client):
        r = client.post(
            "/api/library/upload",
            files={"file": ("我的报告.txt", "营业收入 100 万元".encode())},
        )
        assert r.status_code == 200
        assert r.json()["doc_id"] == "我的报告"
        lib = client.get("/api/library").json()
        assert [x["doc_id"] for x in lib] == ["我的报告"]
        d = client.delete("/api/library/我的报告").json()
        assert sorted(d["removed"]) == ["converted", "original"]
        assert client.get("/api/library").json() == []

    def test_upload_rejects_bad_format(self, client):
        r = client.post(
            "/api/library/upload",
            files={"file": ("virus.exe", b"MZ")},
        )
        assert r.status_code == 400

    def test_ask_free_requires_library(self, client):
        r = client.post("/api/ask-free", json={"question": "收入多少"})
        assert r.status_code == 400

    def test_ask_free_endpoint(self, client):
        client.post(
            "/api/library/upload",
            files={"file": ("report.txt", "公司营业收入 100 万元".encode())},
        )
        r = client.post("/api/ask-free", json={"question": "公司营业收入是多少"})
        assert r.status_code == 200
        d = r.json()
        assert "803,964,958,000.00" in d["answer"] or "100" in d["answer"]
        assert d["total_tokens"] > 0

    def test_ask_free_empty_question(self, client):
        assert client.post("/api/ask-free", json={"question": "  "}).status_code == 400
