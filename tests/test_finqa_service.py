"""finqa Web 服务：考官模式口（gold 不出后端）、归档只读、静态页。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from findata.finqa import service
from findata.finqa.qwen import CallRecord

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _FakeClient:
    def chat(self, qid, purpose, messages):
        reply = "依据【strict_csrc_035 · 第四十七条】。\nVERDICT: TRUE"
        if purpose == "answer":
            reply = "依据第四十七条。\nANSWER: B"
        return reply, CallRecord(
            qid=qid, purpose=purpose, model="fake",
            prompt_tokens=10, completion_tokens=2,
        )


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(service, "build_client", _FakeClient)
    return TestClient(service.app)


def test_index_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "金融长文本智能问答系统" in r.text


def test_questions_hide_gold(client):
    questions = client.get("/api/questions").json()
    assert len(questions) >= 50
    for q in questions:
        assert "gold" not in q and "evidence" not in q
        assert set(q["options"]) >= {"A", "B"}


def test_questions_split_filter(client):
    b = client.get("/api/questions", params={"split": "B"}).json()
    assert b and all(q["split"] == "B" for q in b)


def test_meta(client):
    meta = client.get("/api/meta").json()
    assert meta["questions"] >= 50
    assert meta["b_split"] >= 7


def test_answer_strips_gold(client):
    r = client.post("/api/answer/reg_s_002")
    assert r.status_code == 200
    d = r.json()
    assert "gold" not in d and "correct" not in d
    assert d["answer"] == "B"
    assert any(e["clause"] == "第四十七条" for e in d["evidence"])
    assert d["tokens"]["total"] > 0


def test_answer_unknown_qid_404(client):
    assert client.post("/api/answer/nope").status_code == 404


def test_runs_listing_and_detail(client):
    runs = client.get("/api/runs").json()
    assert runs, "examples/ 应有 AFAC 运行归档"
    name = runs[0]["file"]
    detail = client.get(f"/api/runs/{name}")
    assert detail.status_code == 200


def test_runs_path_traversal_blocked(client):
    assert client.get("/api/runs/..%5Cpyproject.toml").status_code in (400, 404)
    assert client.get("/api/runs/secret.json").status_code == 404
