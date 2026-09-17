"""Prompt 版本化存储（M13）：让 prompt 成为被评测、被门禁管理的资产。

「prompt 也是代码，进化也要过门禁」的落点：
- 每代 prompt 是一个 YAML 文件：模板 + 当时的分数 + 出处（引擎/模型/语料/开销）
- 进化产物先落版本文件，再由 eval-gate 决定能不能用——不过门禁的
  prompt 与不过测试的代码地位相同：不许合入
- 文件即可回滚单元：回退到 v1 = 把 v1 的模板传回 LLMTriage

模板契约（加载时强校验）：
- system 必须含且仅含 {causes} 占位（可选根因白名单由代码注入，不许
  进化自由发挥——根因集合是 schema，不是措辞）
- user 必须含 {asof} 与 {payload}（证据装配是确定性的，不许进化改）
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

REQUIRED_SYSTEM_SLOT = "{causes}"
REQUIRED_USER_SLOTS = ("{asof}", "{payload}")


@dataclass
class PromptVersion:
    """一代 prompt 的完整档案：模板 + 分数 + 出处。"""

    version: str
    target: str
    system: str
    user: str
    metrics: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)
    notes: str = ""

    def validate(self) -> None:
        if not self.version or not self.target:
            raise ValueError("prompt 版本文件缺少 version/target")
        if REQUIRED_SYSTEM_SLOT not in self.system:
            raise ValueError(f"system 模板必须包含 {REQUIRED_SYSTEM_SLOT} 占位")
        for slot in REQUIRED_USER_SLOTS:
            if slot not in self.user:
                raise ValueError(f"user 模板必须包含 {slot} 占位")

    def filename(self) -> str:
        return f"{self.target}_{self.version}.yaml"

    def to_llm_triage_kwargs(self) -> dict:
        """传给 LLMTriage(client, **kw) 的模板覆盖参数。"""
        return {"system": self.system, "user": self.user}


def save_prompt(directory: Path | str, pv: PromptVersion) -> Path:
    """版本落盘。已存在同版本号文件会报错——版本号一旦发布不可覆盖，
    这是「可回滚可对比」的底线。"""
    pv.validate()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / pv.filename()
    if path.exists():
        raise FileExistsError(f"prompt 版本已存在，不可覆盖：{path}")
    payload = {k: v for k, v in asdict(pv).items()}
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False, width=100),
        encoding="utf-8",
    )
    return path


def load_prompt(path: Path | str) -> PromptVersion:
    """加载并强校验一个 prompt 版本文件。模板契约不对，宁可拒载。"""
    path = Path(path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"prompt 文件格式必须是映射：{path}")
    known = {"version", "target", "system", "user", "metrics", "provenance", "notes"}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"prompt 文件含未知字段 {unknown}：{path}")
    pv = PromptVersion(
        version=str(raw.get("version", "")),
        target=str(raw.get("target", "")),
        system=str(raw.get("system", "")),
        user=str(raw.get("user", "")),
        metrics=raw.get("metrics") or {},
        provenance=raw.get("provenance") or {},
        notes=str(raw.get("notes", "")),
    )
    pv.validate()
    return pv


def list_versions(directory: Path | str, target: str = "triage") -> list[PromptVersion]:
    """按版本号数字序列出某目标的全部已归档 prompt（v2 在 v10 之前）。"""
    directory = Path(directory)
    if not directory.exists():
        return []
    out = []
    for path in sorted(directory.glob(f"{target}_*.yaml")):
        try:
            out.append(load_prompt(path))
        except ValueError:
            continue  # 坏文件跳过，不拖垮列举；需要严格场景请逐个 load

    def _seq(p: PromptVersion) -> tuple:
        token = p.version.lstrip("v")
        return (int(token) if token.isdigit() else 0, p.version)

    return sorted(out, key=lambda p: (p.target, _seq(p)))
