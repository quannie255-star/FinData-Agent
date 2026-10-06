"""M5 工具：配对比较与证据导出（离线，用最小运行 json fixture）。"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.compare_afac_runs import _load
from scripts.export_evidence import build_evidence

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _run_json(tmp_path: Path, name: str, preds: dict[str, str], tokens: int) -> Path:
    """构造单 mode 运行归档的最小形态。"""
    details = [
        {
            "qid": qid,
            "domain": "regulatory",
            "answer_format": "mcq",
            "gold": "B",
            "pred": pred,
            "correct": pred == "B",
            "attributions": [
                {
                    "purpose": "judge:B",
                    "fed": [("strict_csrc_035", "第四十七条", 300)],
                    "used_idx": [0],
                }
            ],
            "replies": {"judge:B": "依据【strict_csrc_035 · 第四十七条】，B 正确。VERDICT: TRUE"},
        }
        for qid, pred in preds.items()
    ]
    payload = {
        "runs": {
            "isolate": {
                "report": {
                    "total": len(preds), "correct": sum(p == "B" for p in preds.values()),
                    "accuracy": sum(p == "B" for p in preds.values()) / len(preds),
                    "total_tokens": tokens,
                },
                "details": details,
            }
        }
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class TestCompare:
    def test_load_and_basic_fields(self, tmp_path):
        p = _run_json(tmp_path, "a.json", {"q1": "B", "q2": "A"}, 100)
        run = _load(p)
        assert run["mode"] == "isolate"
        assert set(run["details"]) == {"q1", "q2"}
        assert run["report"]["total_tokens"] == 100


class TestExportEvidence:
    def test_builds_topic_format(self, tmp_path):
        p = _run_json(tmp_path, "a.json", {"q1": "B"}, 100)
        evidence = build_evidence(p, PROJECT_ROOT / "eval/fixtures/afac_scaffold")
        assert len(evidence) == 1
        entry = evidence[0]
        assert entry["qid"] == "q1" and entry["answer"] == "B"
        ev = entry["evidence_retrieval"][0]
        assert ev["doc_id"] == "strict_csrc_035"
        assert "第四十七条" in ev["quoted_clause"]
        assert "担保" in ev["quoted_clause"] or len(ev["quoted_clause"]) > 10
        assert "第四十七条" in ev["reasoning"]

    def test_no_evidence_when_nothing_cited(self, tmp_path):
        p = _run_json(tmp_path, "a.json", {"q1": "B"}, 100)
        payload = json.loads(p.read_text(encoding="utf-8"))
        payload["runs"]["isolate"]["details"][0]["attributions"] = [
            {"purpose": "judge:B", "fed": [("d", "h", 1)], "used_idx": []}
        ]
        p.write_text(json.dumps(payload), encoding="utf-8")
        evidence = build_evidence(p, PROJECT_ROOT / "eval/fixtures/afac_scaffold")
        assert evidence[0]["evidence_retrieval"] == []
