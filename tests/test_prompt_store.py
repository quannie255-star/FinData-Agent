"""Prompt 版本化存储（M13）单测：模板契约校验、版本不可覆盖、列举。"""

import pytest

from findata.agent.prompts import PromptVersion, list_versions, load_prompt, save_prompt


def _pv(version: str = "v1", system: str | None = None, user: str | None = None) -> PromptVersion:
    return PromptVersion(
        version=version,
        target="triage",
        system=system or "你是归因专家。\n\n可选根因（必须原样取自此列表）：\n{causes}",
        user=user or "观察日：{asof}\n\n信号与证据：\n{payload}",
        metrics={"root_cause_accuracy": 0.857},
        provenance={"engine": "hand"},
    )


def test_roundtrip(tmp_path):
    path = save_prompt(tmp_path, _pv())
    loaded = load_prompt(path)
    assert loaded.version == "v1"
    assert loaded.system == _pv().system
    assert loaded.metrics["root_cause_accuracy"] == 0.857


def test_missing_system_slot_rejected(tmp_path):
    bad = _pv(system="没有占位符的模板")
    with pytest.raises(ValueError, match="causes"):
        save_prompt(tmp_path, bad)


def test_missing_user_slot_rejected(tmp_path):
    bad = _pv(user="只有 {asof}，缺 payload")
    with pytest.raises(ValueError, match="payload"):
        save_prompt(tmp_path, bad)


def test_version_overwrite_forbidden(tmp_path):
    save_prompt(tmp_path, _pv("v1"))
    with pytest.raises(FileExistsError):
        save_prompt(tmp_path, _pv("v1"))


def test_unknown_field_rejected_on_load(tmp_path):
    path = save_prompt(tmp_path, _pv())
    import yaml

    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["hacker_field"] = "x"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="未知字段"):
        load_prompt(path)


def test_list_versions_sorted(tmp_path):
    save_prompt(tmp_path, _pv("v2"))
    save_prompt(tmp_path, _pv("v10"))
    save_prompt(tmp_path, _pv("v1"))
    versions = [p.version for p in list_versions(tmp_path)]
    assert versions == ["v1", "v2", "v10"]
