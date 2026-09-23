"""建一份**去泄漏**的审计语料（R5.1 的前置修正）。

为什么需要它（**这是本轮最重要的发现**）
--------------------------------------
判断"模型答对了吗"的判据写在 `src/findata/contextbudget/tasks.py` 里，
而审计语料的根目录**就是本仓库**——也就是说，**考卷和答案卡都放在考场里**。
模型只要读那个文件，答案里就会带上判据本身。

实测（`examples/context-audit/`，冻结批次）：`active7b-fz` 臂有 **5 条**答案引用了
考题定义，其中 **2 条**本来就被判成"命中"——包括 t23：整段在讲 `tasks.py`（跑题），
却引用了 `Task("t23", "…推导…", ("原文",), …)`，问题和判据一起被抄进答案。
**这是语料缺陷，不是判据缺陷。**

为什么不是"改判据"就完了
----------------------
判据侧可以做泄漏检测并剔除（已经在 `grading.has_exam_leak` 做了），但那只是**事后补丁**：
- 它只会**扣分**（不会让任何答案变得更可信）⇒ 剔除本身偏袒泄漏少的那一臂；
- 更根本的是：**一个模型能读到考卷的 benchmark，测出来的不是能力。**

为什么是"复制后删"而不是"删了再跑"
--------------------------------
不能改冻结快照本身：`fz` 那批已归档的读数必须仍然可复现。
所以这里**复制快照 → 删掉含判据的文件 → 记账 → 作为新一批的语料根**。
两批语料各自有指纹，谁都不能冒充谁。

删掉的文件会改变 `list_files` 的返回（比如 `docs/` 下少几个文件）。
**这是必须声明的口径变化**：`manifest.excluded` 逐条列出删了什么，
新批次的所有读数都带这个前提。审计的 32 道题里，判据只要求
`roadmap.md` / `badge.md` 这类仍在的条目，所以不会被删空。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from findata.contextbudget.corpus import corpus_manifest  # noqa: E402

# 含这些标记的文件 = 考卷或判据，一律不进语料。
# 取"标记"而不是具体文件名，是为了让以后新加的判据文件自动被抓住
# ——否则同样的坑会以"新加了一个文件"的形式再踩一次。
EXAM_MARKERS: tuple[str, ...] = (
    "must_contain",
    "StrongSpec(",
    "has_exam_leak",
    "LEAK_FINGERPRINTS",
    "def is_hit(",
)

_SKIP_DIRS = {".git", ".venv", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules"}


def _copy_tree(src: Path, dst: Path) -> int:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(
        src, dst, ignore=shutil.ignore_patterns(*_SKIP_DIRS, "*.pyc"), symlinks=True
    )
    return sum(1 for p in dst.rglob("*") if p.is_file())


def _scan(corpus_root: Path) -> list[dict[str, object]]:
    """找出并删掉含判据标记的文件。返回被删清单。"""
    removed: list[dict[str, object]] = []
    for path in sorted(corpus_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue  # 二进制/读不了的文件不可能泄漏文本判据
        hit = [m for m in EXAM_MARKERS if m in text]
        if not hit:
            continue
        rel = path.relative_to(corpus_root).as_posix()
        removed.append({"path": rel, "chars": len(text), "markers": hit})
        path.unlink()
    return removed


def main() -> int:
    ap = argparse.ArgumentParser(description="建一份去泄漏的审计语料")
    ap.add_argument("--src", required=True, help="源（通常是冻结快照）")
    ap.add_argument("--dst", required=True, help="目标语料根")
    ap.add_argument("--manifest", default="", help="清单落盘路径（.json）")
    args = ap.parse_args()

    src, dst = Path(args.src).resolve(), Path(args.dst).resolve()
    if not src.is_dir():
        print(f"源不存在：{src}")
        return 1

    n_files = _copy_tree(src, dst)
    before = corpus_manifest(dst)
    removed = _scan(dst)
    after = corpus_manifest(dst)

    manifest = {
        "src": str(src),
        "dst": str(dst),
        "n_files_copied": n_files,
        "corpus_before": before,
        "corpus_after": after,
        "excluded": removed,
        "note": (
            "语料 = 冻结快照副本，**删掉所有含判据标记的文件**。"
            "理由：判据写在 contextbudget/tasks.py 里，而语料根就是这个仓库 —— "
            "考卷和答案卡都在考场里。实测 active7b-fz 臂有 5 条答案引用了考题定义，"
            "其中 2 条本来被判成『命中』。"
            "**这是语料缺陷不是判据缺陷**；改语料会改变 list_files 的返回，"
            "所以本批读数与 fz 批次不可直接比，必须各自带自己的语料指纹。"
        ),
    }

    if args.manifest:
        out = Path(args.manifest)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"清单 → {out}")

    print(f"源={src}\n目标={dst}")
    print(f"复制 {n_files} 个文件，删掉 {len(removed)} 个含判据的文件：")
    for item in removed:
        print(f"  - {item['path']}  ({item['chars']:,} 字符, 标记 {item['markers']})")
    print(
        f"语料指纹 {before['sha256']} → {after['sha256']}"
        f"（{before['n_files']} → {after['n_files']} 个文件）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
