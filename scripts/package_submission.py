"""提交物打包：按题面 B 榜代码审核要求组装 submission 目录并压缩。

题面要求的布局（docs/afac-track4.md §0 引用）：

    submission.zip
    ├── answer.csv          # 由运行归档生成（submit.write_answer_csv）
    ├── evidence.json       # evidence_retrieval 格式（export_evidence.build_evidence）
    ├── processed_data/     # 清洗、切分、结构化后的长文本数据（fixture docs+questions）
    ├── agent/              # Agent 系统完整代码（src/findata/finqa）
    ├── script/             # 可复现运行脚本（afac 相关 scripts）
    ├── logs/               # 实验记录（运行归档 txt/json）
    ├── requirements.txt    # uv export 生成
    └── README.md           # 一键运行说明

本脚本是**审核形态的演练**（比赛已收官，无真实提交通道）：验证我们能按
要求一键组装，而不是赛前才发现缺件。
"""

from __future__ import annotations

import argparse
import importlib.util
import shutil
import subprocess
import zipfile
from pathlib import Path

from findata.finqa.submit import write_answer_csv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _load_export_evidence():
    """scripts/ 不是包：直接执行本脚本时按文件路径加载导出模块。"""
    spec = importlib.util.spec_from_file_location(
        "export_evidence", PROJECT_ROOT / "scripts" / "export_evidence.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_evidence


build_evidence = _load_export_evidence()
AFAC_SCRIPTS = [
    "run_afac_baseline.py", "compare_afac_runs.py", "export_evidence.py",
    "build_afac_docs.py", "package_submission.py",
]
README_TMPL = """# AFAC 赛题四 提交物

一键运行（需在 .env 配置 FINDATA_DASHSCOPE_API_KEY）：

    uv run python script/run_afac_baseline.py --mode isolate --out logs/run.txt

- agent/：finqa 包（检索/判定/记忆/归因/提交）
- processed_data/：题集与文档（官方原文确定性提取）
- logs/：随包归档的实验记录
- answer.csv / evidence.json：由归档运行生成（token 为真实台账）

复现口径：同题集、同解码参数（temperature=0, seed 固定），逐配置确定。
"""


def package(run_json: Path, out_dir: Path, schema: str = "5col") -> Path:
    if not run_json.exists():
        raise SystemExit(f"运行归档不存在：{run_json}")
    if out_dir.exists():
        shutil.rmtree(out_dir)
    (out_dir / "processed_data").mkdir(parents=True)
    (out_dir / "agent").mkdir()
    (out_dir / "script").mkdir()
    (out_dir / "logs").mkdir()

    write_answer_csv(run_json, out_dir / "answer.csv", schema=schema)
    evidence = build_evidence(run_json, PROJECT_ROOT / "eval/fixtures/afac_scaffold")
    (out_dir / "evidence.json").write_text(
        __import__("json").dumps(evidence, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    shutil.copytree(
        PROJECT_ROOT / "eval/fixtures/afac_scaffold",
        out_dir / "processed_data" / "afac_scaffold",
    )
    shutil.copytree(
        PROJECT_ROOT / "src/findata/finqa", out_dir / "agent" / "finqa",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name in AFAC_SCRIPTS:
        src = PROJECT_ROOT / "scripts" / name
        if src.exists():
            shutil.copy2(src, out_dir / "script" / name)
    shutil.copy2(run_json, out_dir / "logs" / run_json.name)
    txt = run_json.with_suffix(".txt")
    if txt.exists():
        shutil.copy2(txt, out_dir / "logs" / txt.name)
    # requirements：uv export；失败则留占位说明（不假装有依赖清单）
    req = out_dir / "requirements.txt"
    try:
        result = subprocess.run(
            ["uv", "export", "--no-dev", "--format", "requirements-txt"],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=120,
        )
        req.write_text(result.stdout if result.returncode == 0 else
                       "# 生成失败：请用 `uv sync` 安装（pyproject.toml 为准）\n",
                       encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired):
        req.write_text("# 生成失败：请用 `uv sync` 安装（pyproject.toml 为准）\n",
                       encoding="utf-8")
    (out_dir / "README.md").write_text(README_TMPL, encoding="utf-8")

    zip_path = out_dir.with_suffix(".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(out_dir.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(out_dir.parent))
    return zip_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_json")
    parser.add_argument("--out", default="submission")
    parser.add_argument("--schema", default="5col", choices=("5col", "8col"))
    args = parser.parse_args()
    zip_path = package(PROJECT_ROOT / args.run_json, PROJECT_ROOT / args.out, args.schema)
    print(f"打包完成：{zip_path}（{zip_path.stat().st_size / 1e6:.1f} MB，上限 1GB）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
