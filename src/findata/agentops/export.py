"""按处置分文件流式落盘——质检的**产物**，不只是报告。

为什么单独一个模块而不是写在脚本里：报告是给人看的，样本集是给训练管线
吃的，两者的正确性都得能被单测钉住。写在 `scripts/` 里就没法测。

五档处置各一个文件，不合并成一个带标签的大文件：

  train.jsonl            ✓ 正样本       路径干净且结果正确
  review.jsonl           ⚠️ 需人工       结果对但路径可疑
  discard.jsonl          ✗ 丢弃         agent 能力问题（或虽非其错但不能喂）
  not_failure.jsonl      ⊘ 不算失败      环境问题，**别当负样本**
  schema_defects.jsonl   🔧 待修 schema  数据源的账——样本留着，改工具 schema

第五档是第一版漏掉的。把「工具 schema 写错」也判成样本缺陷并丢弃，等于
拿 agent 的样本量去赔数据源的债：根因在数据源，处置却落在样本上。改法见
`triage.CAT_SCHEMA_CONTRADICTION`。

分开的理由：下游训练脚本通常只认一个目录一个意图。合并成一个文件再靠
标签过滤，等于把「哪些该喂」这个判断推给了下游——而那正是我们卖的东西。
"""

from __future__ import annotations

import collections
import hashlib
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from findata.agentops.triage import (
    RULES_VERSION,
    V_DISCARD,
    V_NOT_FAILURE,
    V_POSITIVE,
    V_REVIEW,
    V_SCHEMA_DEFECT,
    Diagnosis,
)

VERDICT_FILES = {
    V_POSITIVE: "train.jsonl",
    V_REVIEW: "review.jsonl",
    V_DISCARD: "discard.jsonl",
    V_NOT_FAILURE: "not_failure.jsonl",
    V_SCHEMA_DEFECT: "schema_defects.jsonl",
}

# 待修 schema 的聚合产物：**工具级**，不是轨迹级。
# 给数据源方看的是「这 18 个工具的 18 个参数要改」，不是「这 349 条样本不要了」。
TOOL_DEFECTS_FILE = "tool_schema_defects.json"

# 工具 schema 缺陷的两种改法（见 ToolDefectTally 的注释）：
# 一个是"漏了"，一个是"放错了位置"。处置都指向数据源，但动作不同。
DEFECT_UNDECLARED = "缺 default 声明"
DEFECT_MISPLACED = "default 错位"


def defect_kind(detail: str) -> str:
    """从问题原文判断是哪种 schema 缺陷。

    不新增字段来传这个信息，而是从原文推：问题原文是**唯一**的事实来源，
    另开一个字段就多了一处会与原文漂移的地方（本项目吃过这个亏：
    349 条与 353 条的数字在文档里各自为政）。
    """
    return DEFECT_MISPLACED if "错位" in detail or "被标在" in detail else DEFECT_UNDECLARED


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

    @staticmethod
    def _code_commit() -> str:
        """当前代码版本。取不到就标 unknown——**不装作有**。"""
        try:
            r = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                timeout=5,
                cwd=Path(__file__).resolve().parents[3],
            )
            return r.stdout.strip() if r.returncode == 0 else "unknown"
        except (OSError, subprocess.SubprocessError):
            return "unknown"

    def manifest(
        self, n_samples: int, n_calls: int, source: str, data_path: Path | None = None
    ) -> dict[str, Any]:
        return {
            "source": source,
            "n_samples": n_samples,
            "n_calls": n_calls,
            "by_verdict": dict(self.counts),
            "files": {v: VERDICT_FILES[v] for v in VERDICT_FILES},
            # ── 可复现三元组：代码 + 规则 + 数据 ──
            # 缺任何一项，"这批样本是怎么筛出来的"就复现不了。面试被追问过
            # 「规则更新后旧结果怎么复现」，光有 commit 不够——规则常量可能
            # 改了但没提交，落盘的样本照样与代码对不上号。
            "reproducibility": {
                "code_commit": self._code_commit(),
                "rules_version": RULES_VERSION,
                "data": _data_fingerprint(data_path),
            },
            # 口径说明写进产物本身：半年后回头看，不靠人记忆
            "note": (
                "「没查出问题」≠「确认正确」：判定全是确定性查表，"
                "未命中只说明这条轨迹通过了我能查的那几项，不代表它业务上正确。"
                "not_failure 是环境类失败（沙箱超时/工具缺失），**不是负样本**；"
                "schema_defects 是**工具 schema 的缺陷**（description 称有默认值、"
                "schema 未标 default），样本本身不该作废——修完工具 schema 重跑即可。"
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


def _data_fingerprint(path: Path | None) -> dict[str, Any]:
    """数据指纹：**光有代码版本不够，还得知道筛的是哪份数据**。

    公开数据集会被作者重新修订，本地缓存也会被重新下载覆盖。同一套规则跑在
    两版数据上，结果不一样，却查不出"为什么不一样"——除非把数据本身也指纹化。
    这里落 sha256 前 16 位（够区分，又不至于把 manifest 撑长）+ 字节数。

    缺文件时**不抛异常**：manifest 是整轮产物的一部分，不该因为一个诊断字段
    写不出来就让全部样本落盘失败。标 `missing` 让下游自己判断要不要采信。
    """
    if path is None:
        return {"path": None, "sha256_16": None, "bytes": None}
    p = Path(path)
    if not p.is_file():
        return {"path": str(p), "sha256_16": None, "bytes": None, "missing": True}
    h = hashlib.sha256()
    # 分块读：原始语料 96MB，一次性 read 会把内存打上去（同 iter_records 的理由）
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return {"path": str(p), "sha256_16": h.hexdigest()[:16], "bytes": p.stat().st_size}


class ToolDefectTally:
    """把逐条轨迹的 schema 缺陷**聚合成工具级待修清单**。

    为什么必须聚合：349 条轨迹说"349 条样本有问题"，听起来要人工逐条看
    （等于没有队列）；聚合后说"18 个工具、17 个参数要改"，这才是可执行的
    动作。同一份事实，两种表述，一个没用一个有行动。

    这也是"根因与处置分离"的最终落点：根因是数据源的，处置就该作用在
    数据源上，而不是作用在样本上。

    聚合时按**缺陷种类**再分一层，因为改法不一样：

      `缺 default 声明`  description 说有默认值，schema 里找不到这个值
                        → 动作是**补上** default
      `default 错位`     description 说 p 的默认值是 X，X 确实在 schema 里，
                        但标在 q 上 → 动作是**移动**这个值，不是补一个新的

    两者都要动数据源，但一个是"漏了"、一个是"放错了位置"。混成一句
    "补上 default"会让数据源方把 `charge` 和 `permitivity` **同时**标上
    8.854e-12——错得更彻底。
    """

    def __init__(self) -> None:
        self._by_param: dict[tuple[str, str], dict[str, Any]] = {}

    def add(self, tool: str, param: str, detail: str, row: int) -> None:
        key = (tool, param)
        e = self._by_param.setdefault(
            key,
            {
                "tool": tool,
                "param": param,
                "detail": detail,
                "defect_kind": defect_kind(detail),
                "hits": 0,
                "sample_rows": [],
            },
        )
        e["hits"] += 1
        # 只留几行出处：够人工复核，又不把清单撑成第二个数据集。
        # 去重是必须的——同一条轨迹里可能调同一个工具两次，都命中同一个缺陷，
        # 不去重就会出现 `[65, 65, 218]` 这种"三行出处其实是两条轨迹"的假清单。
        if row not in e["sample_rows"] and len(e["sample_rows"]) < 3:
            e["sample_rows"].append(row)

    def to_dict(self, source: str) -> dict[str, Any]:
        entries = sorted(self._by_param.values(), key=lambda x: -x["hits"])
        by_kind = collections.Counter(e["defect_kind"] for e in entries)
        return {
            "source": source,
            "n_tools": len({e["tool"] for e in entries}),
            "n_params": len(entries),
            "n_by_kind": dict(by_kind),
            "action": {
                DEFECT_UNDECLARED: "补上 default（description 已经承诺过了）",
                DEFECT_MISPLACED: "把这个值从 holder 参数**移**到 description 声明的参数上"
                "（不是两边都补——两边都补等于把错固化）",
            },
            "note": "两类都要动数据源、都不要丢样本；但改法不同，别统一成一"
            "句「补 default」。",
            "defects": entries,
        }

    def write(self, out_dir: str | Path, source: str) -> Path:
        p = Path(out_dir) / TOOL_DEFECTS_FILE
        p.write_text(
            json.dumps(self.to_dict(source), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return p
