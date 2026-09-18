"""按处置分文件流式落盘——质检的**产物**，不只是报告。

为什么单独一个模块而不是写在脚本里：报告是给人看的，样本集是给训练管线
吃的，两者的正确性都得能被单测钉住。写在 `scripts/` 里就没法测。

四档处置各一个文件，不合并成一个带标签的大文件：

  train.jsonl       ✓ 正样本      路径干净且结果正确
  review.jsonl      ⚠️ 需人工      结果对但路径可疑
  discard.jsonl     ✗ 丢弃        agent 能力问题（或虽非其错但不能喂）
  not_failure.jsonl ⊘ 不算失败     环境问题，**别当负样本**

分开的理由：下游训练脚本通常只认一个目录一个意图。合并成一个文件再靠
标签过滤，等于把「哪些该喂」这个判断推给了下游——而那正是我们卖的东西。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from findata.agentops.triage import (
    V_DISCARD,
    V_NOT_FAILURE,
    V_POSITIVE,
    V_REVIEW,
    Diagnosis,
)

VERDICT_FILES = {
    V_POSITIVE: "train.jsonl",
    V_REVIEW: "review.jsonl",
    V_DISCARD: "discard.jsonl",
    V_NOT_FAILURE: "not_failure.jsonl",
}


class SplitWriter:
    """流式写：一条一行边生成边落盘，2 万条和 200 万条内存占用一样。"""

    def __init__(self, out_dir: str | Path) -> None:
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.counts: dict[str, int] = {v: 0 for v in VERDICT_FILES}
        self._handles: dict[str, Any] = {}
        for v, name in VERDICT_FILES.items():
            self._handles[v] = (self.out_dir / name).open("w", encoding="utf-8")

    def write(self, verdict: str, obj: dict[str, Any]) -> None:
        h = self._handles.get(verdict)
        if h is None:
            raise ValueError(f"未知处置：{verdict}")
        h.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n")
        self.counts[verdict] += 1

    def attach(self, obj: dict[str, Any], d: Diagnosis, verdict: str) -> dict[str, Any]:
        """把判定结论挂到样本上——**判定随样本走**，不另外维护一张对照表。

        分开存就会漂移（改一行代码忘了同步，样本和结论就对不上）。挂在一起，
        下游拿到样本就知道为什么留/为什么丢，不需要回头查报告。
        """
        obj["findata"] = {
            "verdict": verdict,
            "root_cause": d.category,
            "severity": d.severity,
            "evidence": d.evidence,
            "suggestion": d.suggestion,
        }
        return obj

    def close(self) -> None:
        for h in self._handles.values():
            h.close()
        self._handles = {}

    def __enter__(self) -> SplitWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def manifest(self, n_samples: int, n_calls: int, source: str) -> dict[str, Any]:
        return {
            "source": source,
            "n_samples": n_samples,
            "n_calls": n_calls,
            "by_verdict": dict(self.counts),
            "files": {v: VERDICT_FILES[v] for v in VERDICT_FILES},
            # 口径说明写进产物本身：半年后回头看，不靠人记忆
            "note": (
                "not_failure 是环境类失败（沙箱超时/工具缺失），**不是负样本**；"
                "discard 里的 schema_contradiction 归责数据源，剔除是因为它会教模型"
                "省略必填参数，不等于 agent 做错了。"
            ),
        }

    def write_manifest(self, manifest: dict[str, Any]) -> Path:
        p = self.out_dir / "manifest.json"
        p.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return p


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)
