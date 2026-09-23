"""活跃工具的真实实现 —— 读本项目自己的代码仓库。

**为什么语料用本项目自己的代码**：符合项目一贯的「真实数据优先」铁律。
代码是真实的（80 个文件、有真实的模块结构），不是造出来的；而且零成本、
随时可复现（只要 commit 不变，输入就完全一致）。

**三个工具刻意构成一组对照**（这是整个实验能成立的前提）：
  - `read_file`      → 返回整个文件（大）
  - `search_code`    → 返回命中的**完整函数体**（大）
  - `get_signature`  → 只返回名字与签名（小）

后者的存在是必需的：它证明「同样的信息需求可以用小 payload 满足」。
没有它，"裁掉大 payload" 就会被质疑成"你把功能砍了"。有了它，
同一个问题可以走两条路，代价差多少是**可测的**。

**语料根是可切换的，而且必须可切换**：默认读本仓库工作区，但
`set_corpus_root()` 能把它指向一份**冻结快照**。这不是为了灵活——2026-09-20
实测证明，在跑对照臂的同时往工作区提交文件，会让同一臂的不同任务读到不同的
目录状态（细节见 `corpus.py` 模块头）。**读本地仓库的实验必须在快照上跑**，
否则"唯一变量"这句话是没法核对的。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from findata.contextbudget import corpus
from findata.contextbudget.attribution import FieldRecord
from findata.contextbudget.fields import extract_blocks, record_fields

# `extract_blocks` 现在住在 `fields.py`（切段是"工具返回的形状"这件事的属性，
# 不是经济学分析的属性）。这里再导出一次是**故意的**：旧的 import 路径
# (`from findata.contextbudget.tools import extract_blocks`) 仍然有效。
__all__ = [
    "ACTIVE_IMPL",
    "REPO_ROOT",
    "ToolResult",
    "call_tool",
    "extract_blocks",
    "get_corpus_root",
    "set_corpus_root",
]

# src/findata/contextbudget/tools.py → parents[3] = 仓库根
REPO_ROOT = Path(__file__).resolve().parents[3]

# 当前语料根。用函数读写而不是到处引用常量，是为了让"这次实验读的是哪份语料"
# 只有一个可追踪的入口。
_corpus_root: Path = REPO_ROOT

_MAX_SEARCH_BLOCKS = 20
_MAX_FILE_BYTES = 200_000


def get_corpus_root() -> Path:
    """当前语料根。工具读的就是它下面的文件。"""
    return _corpus_root


def set_corpus_root(path: str | Path | None) -> Path:
    """把语料根指向别处（通常是冻结快照）。传 `None` 复位到本仓库工作区。

    **调用方有责任让这个根在整批实验期间不变**：本函数据此只做一次绝对化，
    不做任何锁定。
    """
    global _corpus_root
    _corpus_root = REPO_ROOT if path is None else Path(path).resolve()
    return _corpus_root


@dataclass
class ToolResult:
    """一次工具调用的结果。

    `payload_chars` 是**确定性**量：序列化后字符数，换机器也一样。
    token 侧的量由 LLM 的 usage 提供，两者都进 trace，但**不许互相冒充**。

    `fields` 是**原生字段级记录**（R5.1）：调用发生时就把返回切成字段、
    记下每个字段的字符数与值指纹。它让"这个字段被引用了吗"**不再需要重放工具**
    ——这是从"只能给自己用"到"能给别人用"的那一步，理由见 `fields.py` 模块头。
    由 `call_tool` 填充；直接构造的 ToolResult 里它是空的（读作"未记录"，不是"没字段"）。
    """

    name: str
    content: str
    result_code: str = "ok"
    payload_chars: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    fields: tuple[FieldRecord, ...] = ()

    def __post_init__(self) -> None:
        if not self.payload_chars:
            self.payload_chars = len(self.content)


def _resolve(rel_path: str) -> Path | None:
    """把语料根相对路径解析成绝对路径，并挡住目录穿越。"""
    root = get_corpus_root()
    candidate = (root / rel_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _iter_source_files(prefix: str = "") -> list[Path]:
    """语料内的源文件。**边界定义只有一处**：`corpus.iter_corpus_files`。

    如果这里另写一份 glob，"指纹算到的语料"和"工具读到的语料"就会分家，
    而这份指纹的全部意义就是描述后者。
    """
    if prefix and _resolve(prefix) is None:
        return []
    return corpus.iter_corpus_files(get_corpus_root(), prefix)


def _rel(path: Path) -> str:
    return path.resolve().relative_to(get_corpus_root()).as_posix()


def _signature_summary(path: Path) -> str:
    """用 ast 抽出模块级定义的名字、参数表、docstring 首行。没有函数体。"""
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return f"[unreadable: {exc}]"

    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return f"[unparsable: {exc}]"

    parts: list[str] = []
    module_doc = ast.get_docstring(tree)
    if module_doc:
        parts.append(f'"""{module_doc.splitlines()[0]}"""')

    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            parts.append(f"{prefix} {node.name}({_args_of(node)})")
        elif isinstance(node, ast.ClassDef):
            parts.append(f"class {node.name}:")
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    prefix = "async def" if isinstance(item, ast.AsyncFunctionDef) else "def"
                    parts.append(f"    {prefix} {item.name}({_args_of(item)})")
    return "\n".join(parts)


def _args_of(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    args = node.args
    names = [a.arg for a in args.posonlyargs + args.args + args.kwonlyargs]
    if args.vararg:
        names.append(f"*{args.vararg.arg}")
    if args.kwarg:
        names.append(f"**{args.kwarg.arg}")
    return ", ".join(names)


# --------------------------------------------------------------------------
# 工具实现（名字与 schemas.ACTIVE_TOOLS 一一对应）
# --------------------------------------------------------------------------


def search_code(query: str, max_results: int = 5) -> ToolResult:
    """返回命中的完整函数/类块（这一版刻意不截断——大 payload 正是观测对象）。"""
    if not query.strip():
        return ToolResult("search_code", "query must not be empty", "error")

    hits: list[tuple[str, str, str]] = []  # (路径, 名字, 块原文)
    for path in _iter_source_files():
        if len(hits) >= _MAX_SEARCH_BLOCKS:
            break
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if query not in source:
            continue
        for name, _start, _end, text in extract_blocks(source):
            if query in text:
                hits.append((_rel(path), name, text))
                if len(hits) >= _MAX_SEARCH_BLOCKS:
                    break

    if not hits:
        return ToolResult("search_code", f"No matches for {query!r}.", "not_found")

    want = max(1, min(int(max_results or 5), len(hits)))
    shown = hits[:want]
    chunks = [
        f"### {rel} :: {name}\n```python\n{text}\n```" for rel, name, text in shown
    ]
    header = (
        f"{len(hits)} match(es) for {query!r}; showing {len(shown)} with full bodies.\n\n"
    )
    return ToolResult(
        "search_code",
        header + "\n\n".join(chunks),
        "ok",
        detail={"n_matches_total": len(hits), "n_matches_shown": len(shown), "query": query},
    )


def read_file(path: str) -> ToolResult:
    """返回整个文件（大 payload 的主要来源）。"""
    target = _resolve(path)
    if target is None or not target.is_file():
        return ToolResult("read_file", f"File not found: {path}", "not_found")
    if target.stat().st_size > _MAX_FILE_BYTES:
        return ToolResult("read_file", f"File too large: {path}", "error")
    try:
        text = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ToolResult("read_file", f"Unreadable: {exc}", "error")
    return ToolResult(
        "read_file",
        f"```python\n{text}\n```",
        "ok",
        detail={"path": _rel(target), "n_lines": text.count("\n") + 1},
    )


def get_signature(path: str) -> ToolResult:
    """只返回模块的公开界面（小 payload 的对照路径）。"""
    target = _resolve(path)
    if target is None or not target.is_file():
        return ToolResult("get_signature", f"File not found: {path}", "not_found")
    summary = _signature_summary(target)
    return ToolResult(
        "get_signature",
        f"# {path}\n{summary}",
        "ok",
        detail={"path": path},
    )


def list_files(prefix: str = "") -> ToolResult:
    """列出源码路径（只有路径，没有内容）。"""
    files = _iter_source_files(prefix or "")
    if not files:
        return ToolResult("list_files", f"No files under {prefix!r}.", "not_found")
    return ToolResult(
        "list_files",
        "\n".join(_rel(p) for p in files),
        "ok",
        detail={"n_files": len(files)},
    )


ACTIVE_IMPL = {
    "search_code": search_code,
    "read_file": read_file,
    "get_signature": get_signature,
    "list_files": list_files,
}


def _valid_param_names(name: str) -> tuple[str, ...] | None:
    """从工具定义里取合法参数名 —— 单一来源，避免与 schema 漂移。

    局部 import 是为了避免 tools ↔ schemas 出现顶层循环依赖。
    """
    from findata.contextbudget.schemas import ACTIVE_TOOLS

    for tool in ACTIVE_TOOLS:
        fn = tool["function"]
        if fn["name"] == name:
            props = (fn.get("parameters") or {}).get("properties") or {}
            return tuple(props.keys())
    return None


def call_tool(
    name: str, arguments: dict[str, Any], *, before_text: str = ""
) -> ToolResult:
    """统一入口：活跃工具走真实实现，沉默工具如实回 not_implemented。

    **参数名预检是刻意加的**，不是防御性编程：实测 qwen2.5:3b 在 27 个工具
    的清单下会把 `prefix` 传成 `"path: "` 这种自造键。如果只回
    `unexpected keyword argument`，模型拿不到任何可执行信息，只能瞎猜下一轮
    —— 那一轮的成本就白花了。所以这里回**合法参数名清单**，给它一次自修
    的机会（也就是 appscale 那篇 production pattern 里的 repair loop）。

    自修失败也算数据：失败率本身就是「工具清单过大导致协议遵循退化」的
    一个可观测量。但**错误信息必须可行动**，否则我观测到的是我自己制造的
    障碍，不是真实世界的障碍。
    """
    impl = ACTIVE_IMPL.get(name)
    if impl is None:
        return ToolResult(
            name,
            f"tool_not_implemented: {name} is registered but has no implementation "
            f"in this environment.",
            "not_implemented",
        )

    valid = _valid_param_names(name)
    if valid is not None:
        unknown = sorted(key for key in arguments if key not in valid)
        if unknown:
            return ToolResult(
                name,
                f"bad_arguments: unknown parameter(s) {unknown}. "
                f"Valid parameters for {name}: {list(valid)}. "
                f"Retry with the correct parameter names.",
                "error",
                detail={"unknown": unknown, "valid": list(valid)},
            )

    try:
        result = impl(**arguments)
    except TypeError as exc:
        result = ToolResult(name, f"bad_arguments: {exc}", "error")
    # 原生字段级记录：**在这一刻**记，不靠事后重放（理由见 fields.py 模块头）。
    return _with_fields(result, before_text=before_text)


def _with_fields(result: ToolResult, *, before_text: str) -> ToolResult:
    """给结果补上字段记录表。

    `before_text` 为空时**照样记录**，但那时"可区分值"没扣过上下文——
    所以记下的 `tokens` 会偏多（引用率偏高的方向）。
    调用方（`agent.run_agent`）总是传真实上下文；这里不抛异常，
    是因为"记录不全"应当表现为一个**可观测的数字**（后续统计里能看出来），
    而不是让整批实验跑不起来。
    """
    result.fields = record_fields(
        result.name,
        result.content,
        result_code=result.result_code,
        before_text=before_text,
    )
    return result
