"""语料指纹：让「两臂是不是同一个语料」从口头声明变成**可核对的数字**。

**为什么需要这个模块**（2026-09-20 实测踩坑，见 `docs/r5.0-acceptance.md` §3.5）：

R5.0 的 harness 读的是**活的本地仓库**。我在跑第一臂（`all7b`）的同时往仓库里
提交了 4 个提交，于是那一臂的 `list_files` 三次调用**读到了三个不同的目录状态**
（`docs/` 少一个文件、`scripts/` 少两个、`tests/` 少三个）。事后核对：

| 调用 | 臂 A 实测 chars | 对应提交 | 臂 B 实测 chars |
| --- | --- | --- | --- |
| `list_files docs/`    | 287 | 早于 `a16da4c` | 311 |
| `list_files scripts/` | 801 | `dc5a022` | 865 |
| `list_files tests/`   | 833 | 早于 `a16da4c` | 916 |

而 `compare_audit_arms.py` 当时**印着**「唯一变量是工具清单长度」——**这句话是假的**，
而且没有任何产物能证伪它。这就是本模块要消灭的东西。

设计原则：

1. **指纹是内容的函数，不是时间的函数**：`sha256(路径 → 内容 sha256)` 的归并。
   同一份内容换台机器、换个目录都得到同一个指纹。
2. **指纹只记聚合值**，不记每个文件的原文（账单体积可控）。
   需要逐文件对比时用 `diff_manifests()`，它单独算。
3. **绝不假设本模块给出的"一致"等于"实验有效"**：它只证明**语料相同**。
   模型行为、采样随机性、并发都不在它的管辖范围内。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

__all__ = [
    "CORPUS_EXCLUDED_DIRS",
    "CORPUS_SUFFIXES",
    "corpus_manifest",
    "diff_manifests",
    "iter_corpus_files",
    "manifests_agree",
]

# 工具的语料边界。**改这两个常量等于改实验语料**，必须同步 `RULES_VERSION`
# 式的口径标注（见 docs/r5.0-acceptance.md 的复现三元组）。
CORPUS_SUFFIXES: tuple[str, ...] = (".py", ".md", ".toml", ".yaml", ".yml")

CORPUS_EXCLUDED_DIRS: frozenset[str] = frozenset(
    {".venv", ".git", "__pycache__", "node_modules"}
)


def iter_corpus_files(root: Path, prefix: str = "") -> list[Path]:
    """列出语料内的文件（**相对 root 的路径，已排序**）。

    `prefix` 为空时扫全库；给前缀时只扫该子目录（路径不存在则返回空表）。
    排序是**契约的一部分**：指纹依赖顺序稳定。

    前缀解析后会做一次**包含性检查**：`../` 这类前缀解析出 root 之外时返回空表，
    不抛异常 —— 语料边界是不变量，越界的输入当作"不在语料内"。
    """
    root = root.resolve()
    base = (root / prefix).resolve() if prefix else root
    if prefix:
        try:
            base.relative_to(root)
        except ValueError:
            return []
    if base.is_file():
        return [base] if base.suffix in CORPUS_SUFFIXES else []
    if not base.is_dir():
        return []

    found: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix not in CORPUS_SUFFIXES:
            continue
        if any(part in CORPUS_EXCLUDED_DIRS for part in path.parts):
            continue
        found.append(path)
    return sorted(found)


def _file_digest(path: Path) -> str:
    """单文件内容 sha256 的**前 16 位**（够用且短；不是安全性用途）。"""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    return hasher.hexdigest()[:16]


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def corpus_manifest(root: Path) -> dict[str, Any]:
    """语料指纹。返回**聚合值**：

    - `sha256`：把 `"<相对路径>:<内容 sha256>"` 按路径排序后拼起来再哈希
    - `n_files` / `bytes`：规模；两者都变才说明内容真变了，只看一个会误判
      （改名不改内容 → `n_files`/`bytes` 不变但 `sha256` 变）

    语料为空时 `sha256` 是空串的哈希，不是 `None` —— **"空"和"没测"必须能区分**。
    """
    files = iter_corpus_files(root)
    lines: list[str] = []
    total_bytes = 0
    for path in files:
        total_bytes += path.stat().st_size
        lines.append(f"{_rel(root, path)}:{_file_digest(path)}")
    payload = "\n".join(sorted(lines)).encode("utf-8")
    return {
        "sha256": hashlib.sha256(payload).hexdigest()[:32],
        "n_files": len(files),
        "bytes": total_bytes,
    }


def diff_manifests(root_a: Path, root_b: Path) -> dict[str, list[str]]:
    """逐文件对比两个语料，回答"差在哪"而不是只说"不一样"。

    返回 `{"added": [...], "removed": [...], "changed": [...]}`（相对路径）。
    `changed` 用内容 sha256 判，不看 mtime —— mtime 会因为 checkout/复制而变。
    """
    map_a = {_rel(root_a, p): _file_digest(p) for p in iter_corpus_files(root_a)}
    map_b = {_rel(root_b, p): _file_digest(p) for p in iter_corpus_files(root_b)}

    return {
        "added": sorted(set(map_b) - set(map_a)),
        "removed": sorted(set(map_a) - set(map_b)),
        "changed": sorted(k for k in set(map_a) & set(map_b) if map_a[k] != map_b[k]),
    }


def manifests_agree(manifests: Iterable[dict[str, Any]]) -> bool | None:
    """多个指纹是否一致。

    返回 `None` 表示**无法判断**（有指纹缺失）—— 刻意不用 `False` 表示缺数据：
    「不一致」和「不知道」是两件事，混起来就会把老归档误判成"语料不同"。
    """
    digests = [m.get("sha256") for m in manifests]
    if not digests or any(not d for d in digests):
        return None
    return len(set(digests)) == 1
