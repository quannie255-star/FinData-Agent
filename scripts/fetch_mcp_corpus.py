"""把**真实公开 MCP server 的工具定义**抓成本项目的工具清单。

为什么需要它（这是全项目最像 demo 的一处）
------------------------------------------
`contextbudget/schemas.py` 里那 27 个工具，**4 个是真实实现，其余 23 个是我手写的**
（`SILENT_TOOLS`，名字照真实生态的形态取的）。这有两个后果：

1. 任何"工具定义占 prompt 多少"的比例，都建立在**我写的定义**上，
   所以只能读成"实验条件下的量"，不能当行业事实；
2. 面试里这是**挡不住的一问**——"你的 27 个工具哪来的？" 答"我写的"，
   后面所有数字的可外推性就都成了待讨论项。

本脚本的作用就是把这一步换成可核对的事实：抓真实 server 的定义，并**记下来源**。

两类来源，证据强度**不一样**，所以分开标注、分开报数
---------------------------------------------------
- `page_declared`：别人仓库里**声明的** schema 文件（如 `oslook/mcp-servers-schemas`）。
  这是"有人写在文件里"的声明，可能与 server 实际报的不一致。
- `introspected`：本机对真 server 走 stdio 发 `tools/list`，拿它**当场报**的定义。
  这是最硬的一类证据，但它要求那个 server 在本机能跑起来。

**绝不允许**把两类混成一个数上报，也**绝不允许**把"抓不到"记成"这个 server 有 0 个工具"——
抓不到就是抓不到，单列，报失败率。这条是本项目的老规矩：
"未观测" ≠ "不存在"。

速率限制（会咬人的地方）
------------------------
`api.github.com` 未鉴权只有 **60 次/小时**，而列目录 + 逐个取文件很容易超。
所以文件体有两条路可走：`raw.githubusercontent.com`（不吃额度）与 GitHub API 的
raw 端点（吃额度），**走哪条要逐个记进产物**，另一条失败的原因也一起记 ——
否则"这份叶子是从哪来的、这次抓取离限流还有多远"就都说不清。

> **本机实测（2026-09-22）**：`raw.githubusercontent.com` **读超时**（curl 也报 56/23），
> 而 `api.github.com` 正常（200）。所以默认顺序是 api → raw，可用 `--prefer` 切回。
> 这是**这台机器上的观测**，不是"哪条更好"的结论。

可复现
------
每个源文件都落盘缓存（`--cache-dir`）并记 sha256；`--offline` 只读缓存。
工具清单指纹与 `run_context_audit.build_reproducibility` **同一算法**
（`sha256(json.dumps(tools, sort_keys=True))`），所以这里算出来的值
可以直接和账单里的 `tools_list_sha256` 对比 —— 两条独立路径互相核得上，
才说明"实验用的就是这份清单"。

用法
----
    # 阶段一：47 个真实 server 的声明式 schema
    uv run python scripts/fetch_mcp_corpus.py --source schemas \
        --out examples/mcp-corpus/schemas-github.json

    # 阶段二：本机真 server 当场 introspection（成功率如实报）
    uv run python scripts/fetch_mcp_corpus.py --source introspect \
        --server "npx -y @modelcontextprotocol/server-filesystem ." \
        --out examples/mcp-corpus/introspected.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_REPO = "oslook/mcp-servers-schemas"
DEFAULT_SUBDIR = "schemas"
DEFAULT_CACHE = Path("examples/mcp-corpus/raw")

SOURCE_PAGE = "page_declared"
SOURCE_INTROSPECTED = "introspected"

# 常见 npm 上的 MCP server（阶段二的候选）。**不是**"这些一定可用"的承诺——
# 能不能跑起来由成功率说话，本列表只提供输入。
DEFAULT_SERVERS: list[str] = [
    "npx -y @modelcontextprotocol/server-filesystem .",
    "npx -y @modelcontextprotocol/server-memory",
    "npx -y @modelcontextprotocol/server-everything",
    "npx -y @modelcontextprotocol/server-sequential-thinking",
    "npx -y @modelcontextprotocol/server-time",
    "npx -y @modelcontextprotocol/server-fetch",
]


# ---------------------------------------------------------------------------
# 工具定义：统一成 OpenAI function 形状（实验台吃的就是这个）
# ---------------------------------------------------------------------------

# `input_schema` / `inputSchema` / `parameters` 在真实语料里都见过。
# 这是**表示层方言**，不是"三种不同的东西"——但数出各占多少本身是有效的信息
# （说明"这份语料的形状有多不统一"，会影响任何跨 server 的自动处理）。
_SCHEMA_KEYS = ("input_schema", "inputSchema", "parameters")


def schema_key_of(raw: dict[str, Any]) -> str:
    """这条定义用的是哪个 schema 键（`input_schema` / `inputSchema` / `parameters`）。

    三种写法在真实语料里都存在，这是**表示层方言**，不是"三种不同的东西"。
    数出各占多少本身有用：它决定这份语料能不能被同一个程序批量处理。
    """
    for key in _SCHEMA_KEYS:
        if isinstance(raw.get(key), dict):
            return key
    return ""


def to_openai_tool(raw: dict[str, Any]) -> dict[str, Any] | None:
    """一条原始 MCP 工具定义 → OpenAI function 形状。**缺名字就返回 None。**

    缺名字时**不许编一个**（比如用位置序号命名）：那会把"这条定义不完整"
    变成"这条定义叫 xxx"，是造数据。返回 None，由调用方计数并报出来。

    ⚠️ **返回的字典里不带任何来源字段，这是有意的**：语料里的 `tools`
    要能**原样喂给 runner**，而账单里 `tools_list_sha256` 算的正是喂进去的那份。
    来源若混在工具里，两个指纹就永远对不上 —— 而且**没有任何东西会报错**。
    来源单独存进 `tool_index`，与工具列表按下标对齐。
    """
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    key = schema_key_of(raw)
    schema = raw.get(key) if key else None
    if not isinstance(schema, dict):
        schema = {"type": "object", "properties": {}}
    props = schema.get("properties")
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": str(raw.get("description") or ""),
            "parameters": {
                "type": str(schema.get("type") or "object"),
                "properties": props if isinstance(props, dict) else {},
                "required": list(schema.get("required") or []),
            },
        },
    }


def corpus_fingerprint(tools: list[dict[str, Any]]) -> str:
    """与账单里 `tools_list_sha256` **同一算法、同一输入**。

    `run_context_audit.build_reproducibility` 算的是
    `sha256(json.dumps(tools, sort_keys=True, ensure_ascii=False))[:32]`，
    其中 `tools` 就是发给模型的那份清单；这里算的是**语料里那份清单**。
    两者能对上，靠的是这份清单里**没有夹带来源字段**（见 `to_openai_tool`）。

    对上这件事的意义：它把"实验跑的就是这份语料"从一句声明变成一次核对
    —— 语料指纹与账单指纹是两份独立落盘的东西，对不上就说明中间被换过。
    """
    blob = json.dumps(tools, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def tools_payload_chars(tools: list[dict[str, Any]]) -> int:
    """工具定义序列化后的字符数（与 `schemas.tools_payload_chars` 同口径）。

    **这是字符，不是 token。** token 必须在真实请求上实测（见 `probe.py`），
    拿字符数乘一个系数换算出来的数不许当实测值报。
    """
    return len(json.dumps(tools, ensure_ascii=False))


# ---------------------------------------------------------------------------
# 阶段一：抓"声明式 schema"
# ---------------------------------------------------------------------------


def _api_get(url: str, *, timeout: float = 30.0) -> bytes:
    """GET 一个 URL，返回响应体。用 httpx 是为了复用项目已有的依赖。"""
    import httpx

    resp = httpx.get(url, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _api_list_dir(repo: str, subdir: str) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/contents/{subdir}"
    payload = json.loads(_api_get(url).decode("utf-8"))
    if isinstance(payload, dict):  # 出错时 GitHub 返回 {"message": ...}
        raise RuntimeError(f"列目录失败：{payload.get('message')}")
    return [e for e in payload if e.get("type") == "file"]


def fetch_source_file(
    entry: dict[str, Any],
    *,
    repo: str,
    subdir: str,
    cache: Path,
    offline: bool,
    prefer: str = "api",
    http_timeout: float = 20.0,
) -> tuple[bytes, str, list[str]]:
    """取一个源文件的字节。返回 `(内容, 走的哪条路, 被放弃的那条路失败的原因)`。

    **两条路，且要说清走了哪条、另一条为什么没走**：

    - `api`：`api.github.com` 的 contents 端点，带 `Accept: application/vnd.github.raw`。
      稳定，但**吃 60 次/小时的未鉴权额度**。
    - `raw`：`raw.githubusercontent.com`，不吃额度。

    本机实测（2026-09-22）：**`raw` 读超时，`api` 正常**（curl 对 raw 也报
    56/23，对 api 报 200）。所以默认顺序是 api → raw。这不是"哪个更好"的
    结论，是**这台机器上的观测**，所以用 `prefer` 留了切换口。

    只报"抓到了"是不够的：读者要能判断"这次抓取离限流还有多远"，
    所以没走成的路也要把原因带回产物里。
    """
    name = str(entry["name"])
    cached = cache / name
    if cached.exists():
        return cached.read_bytes(), "cache", []
    if offline:
        raise RuntimeError(f"offline 模式但缓存里没有 {name}")
    api = (
        "api",
        f"https://api.github.com/repos/{repo}/contents/{subdir}/{name}",
        {"Accept": "application/vnd.github.raw"},
    )
    raw = ("raw", f"https://raw.githubusercontent.com/{repo}/main/{subdir}/{name}", None)
    order = [api, raw] if prefer == "api" else [raw, api]

    errors: list[str] = []
    for via, url, headers in order:
        try:
            if headers is None:
                body = _api_get(url, timeout=http_timeout)
            else:
                import httpx

                resp = httpx.get(
                    url, headers=headers, timeout=http_timeout, follow_redirects=True
                )
                resp.raise_for_status()
                body = resp.content
        except Exception as exc:  # noqa: BLE001 —— 记下原因继续试下一条路
            errors.append(f"{via}: {type(exc).__name__}: {exc}")
            continue
        # 建缓存目录**本身**（不只是它父目录）：省掉一整类
        # "父目录建了、自己没有" 的 `FileNotFoundError`。
        cache.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(body)
        return body, via, errors
    raise RuntimeError(f"{name} 两条路都失败 → " + " | ".join(errors))


def normalize_declared(
    payload: dict[str, Any], *, server_id: str, provenance: dict[str, Any]
) -> dict[str, Any]:
    """一份声明式 schema → 本项目的 server 记录。

    `server_info` 里的键名在真实语料里就有 camelCase / snake_case 两种写法
    （`repoUrl` vs `repo_url`）。两种都读，并**记下用的是哪种** ——
    这是表示层方言，不该被当成"两种不同的 server"。
    """
    info = payload.get("server_info") or {}
    is_camel = any(k[:1].islower() and any(c.isupper() for c in k) for k in info)
    dialect = "camel" if is_camel else "snake"
    raw_tools = payload.get("tools") or []
    tools: list[dict[str, Any]] = []
    schema_keys: list[str] = []
    n_no_name = 0
    for item in raw_tools:
        if not isinstance(item, dict):
            n_no_name += 1
            continue
        tool = to_openai_tool(item)
        if tool is None:
            n_no_name += 1
            continue
        tools.append(tool)
        schema_keys.append(schema_key_of(item))
    return {
        "id": server_id,
        "declared_name": info.get("name"),
        "repo_url": info.get("repo_url") or info.get("repoUrl") or "",
        "server_type": info.get("server_type") or info.get("serverType") or "",
        "source_kind": SOURCE_PAGE,
        "info_dialect": dialect,
        "n_tools": len(tools),
        "n_tools_without_name": n_no_name,
        "n_resources": len(payload.get("resources") or []),
        "n_prompts": len(payload.get("prompts") or []),
        "tools": tools,
        "tool_schema_keys": schema_keys,
        "provenance": provenance,
    }


def collect_schemas(
    *,
    repo: str,
    subdir: str,
    cache: Path,
    offline: bool,
    limit: int = 0,
    prefer: str = "api",
    http_timeout: float = 20.0,
) -> dict[str, Any]:
    started = time.time()
    entries = _api_list_dir(repo, subdir)
    if limit:
        entries = entries[:limit]
    servers: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for entry in entries:
        name = str(entry["name"])
        server_id = name[: -len(".json")] if name.endswith(".json") else name
        try:
            body, via, skipped = fetch_source_file(
                entry,
                repo=repo,
                subdir=subdir,
                cache=cache,
                offline=offline,
                prefer=prefer,
                http_timeout=http_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append({"file": name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            failures.append({"file": name, "error": f"不是合法 JSON：{exc}"})
            continue
        servers.append(
            normalize_declared(
                payload,
                server_id=server_id,
                provenance={
                    "file": name,
                    "via": via,
                    # 没走成的那条路为什么没走成 —— 决定"离限流还有多远"要看的正是它
                    "skipped_paths": skipped,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                },
            )
        )
    return {
        "source": f"github:{repo}/{subdir}",
        "source_kind": SOURCE_PAGE,
        "servers": servers,
        "failures": failures,
        "n_source_files": len(entries),
        "elapsed_s": round(time.time() - started, 2),
    }


# ---------------------------------------------------------------------------
# 阶段二：本机真 server 的 introspection
# ---------------------------------------------------------------------------


async def _introspect_one(command: str, *, timeout: float) -> dict[str, Any]:
    """对一个真 server 走 stdio 发 `tools/list`。**如实返回成功或失败。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parts = command.split()
    params = StdioServerParameters(command=parts[0], args=parts[1:], env=None)

    async def _session() -> dict[str, Any]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                tools: list[dict[str, Any]] = []
                for item in listed.tools:
                    tool = to_openai_tool(
                        {
                            "name": item.name,
                            "description": item.description or "",
                            "inputSchema": getattr(item, "inputSchema", None),
                        }
                    )
                    if tool is not None:
                        tools.append(tool)
                return {"n_tools": len(tools), "tools": tools}

    try:
        result = await asyncio.wait_for(_session(), timeout=timeout)
    except Exception as exc:  # noqa: BLE001 —— 失败要如实记，不许吞
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "n_tools": None,
            "tools": [],
        }
    return {"ok": True, "error": "", **result}


def collect_introspected(commands: list[str], *, timeout: float) -> dict[str, Any]:
    started = time.time()
    servers: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for command in commands:
        result = asyncio.run(_introspect_one(command, timeout=timeout))
        if not result["ok"]:
            failures.append({"command": command, "error": result["error"]})
            continue
        servers.append(
            {
                "id": command,
                "declared_name": command,
                "source_kind": SOURCE_INTROSPECTED,
                "n_tools": result["n_tools"],
                "tools": result["tools"],
                "provenance": {"command": command, "via": "stdio tools/list"},
            }
        )
    return {
        "source": "local stdio introspection",
        "source_kind": SOURCE_INTROSPECTED,
        "servers": servers,
        "failures": failures,
        "n_source_files": len(commands),
        "elapsed_s": round(time.time() - started, 2),
    }


# ---------------------------------------------------------------------------
# 汇总与落盘
# ---------------------------------------------------------------------------


def build_corpus(collected: dict[str, Any], *, note: str = "") -> dict[str, Any]:
    servers = collected["servers"]
    # `tools` 保持**干净**（可直接喂给 runner，指纹与账单同源）；
    # 来源信息按下标存在 `tool_index` 里。两者长度必须相等 —— 这样"对齐"
    # 是一个可断言的事实，而不是"我记得它们是对齐的"。
    flat: list[dict[str, Any]] = []
    index: list[dict[str, Any]] = []
    for server in servers:
        keys = server.get("tool_schema_keys") or []
        for pos, tool in enumerate(server["tools"]):
            flat.append(tool)
            index.append(
                {
                    "i": len(flat) - 1,
                    "server": server["id"],
                    "source_kind": server["source_kind"],
                    "schema_key": keys[pos] if pos < len(keys) else "",
                }
            )
    assert len(flat) == len(index)
    n_no_name = sum(int(s.get("n_tools_without_name") or 0) for s in servers)
    dialects = sorted({s["info_dialect"] for s in servers if s.get("info_dialect")})
    schema_key_counts: dict[str, int] = {}
    for item in index:
        key = item["schema_key"] or "(无 schema)"
        schema_key_counts[key] = schema_key_counts.get(key, 0) + 1
    by_kind: dict[str, int] = {}
    for server in servers:
        by_kind[server["source_kind"]] = by_kind.get(server["source_kind"], 0) + 1
    return {
        "meta": {
            "source": collected["source"],
            "source_kind": collected["source_kind"],
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "n_source_files": collected["n_source_files"],
            "n_servers_ok": len(servers),
            "n_servers_failed": len(collected["failures"]),
            "servers_by_source_kind": by_kind,
            "n_tools": len(flat),
            "n_tools_without_name": n_no_name,
            "info_dialects": dialects,
            "schema_key_counts": schema_key_counts,
            "tools_list_sha256": corpus_fingerprint(flat),
            "tools_payload_chars": tools_payload_chars(flat),
            "failures": collected["failures"],
            "note": (
                "工具定义来自真实公开 server。`source_kind` 区分两类证据："
                "`page_declared` = 别人仓库里**声明**的 schema（可能与 server 实际报的不同），"
                "`introspected` = 本机对真 server 走 stdio `tools/list` **当场报**的。"
                "**两者不可混报**，也**不可把抓失败记成'该 server 有 0 个工具'**："
                "失败单列在 failures 里。"
                "`tools` 里**没有来源字段**（来源在 `tool_index`，按下标对齐），"
                "所以这份 `tools` 可以原样喂给 runner，账单里的 `tools_list_sha256` "
                "应当等于 meta 里的同名值 —— 这是两条独立路径的一次核对。"
                "tools_payload_chars 是**字符**，token 必须在真实请求上实测。"
                + (f" {note}" if note else "")
            ),
        },
        "servers": servers,
        "tools": flat,
        # 与 `tools` **按下标对齐**。工具本身是干净的（没有来源字段），
        # 来源住在这里 —— 于是"第 i 个工具来自哪台 server、哪一类证据"是一条
        # 可断言的事实（`len(tool_index) == len(tools)`），而不是我记性好。
        "tool_index": index,
    }


def summarize(corpus: dict[str, Any]) -> None:
    meta = corpus["meta"]
    print("=" * 88)
    print("真实 MCP server 工具语料")
    print("=" * 88)
    print(f"  来源            : {meta['source']}")
    print(f"  证据类型        : {meta['source_kind']}")
    print(
        f"  源文件          : {meta['n_source_files']}"
        f"  →  成功 {meta['n_servers_ok']} / 失败 {meta['n_servers_failed']}"
    )
    if meta["n_source_files"]:
        rate = meta["n_servers_ok"] / meta["n_source_files"]
        print(
            f"  **成功率**      : {rate:.1%}  "
            f"（{meta['n_servers_ok']}/{meta['n_source_files']}）"
        )
    print(f"  工具总数        : {meta['n_tools']}")
    if meta["n_tools_without_name"]:
        print(f"  ⚠ 缺名字被跳过的: {meta['n_tools_without_name']}（**不许编名字补上**）")
    print(f"  定义字符数      : {meta['tools_payload_chars']:,}（**字符，不是 token**）")
    print(f"  清单指纹        : {meta['tools_list_sha256']}")
    if meta["info_dialects"]:
        print(f"  server_info 方言: {meta['info_dialects']}（表示层差异，不是两类 server）")
    if meta["failures"]:
        print(f"  ⚠ 失败 {len(meta['failures'])} 个（**不算 0 个工具**）：")
        for item in meta["failures"][:10]:
            print(f"      - {item}")
    if not meta["n_tools"]:
        print("  ✗ **一个工具都没抓到** ⇒ 这次抓取什么都没证明，不许往下用")


def main() -> int:
    ap = argparse.ArgumentParser(description="抓真实 MCP server 的工具定义")
    ap.add_argument("--source", choices=("schemas", "introspect"), default="schemas")
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--subdir", default=DEFAULT_SUBDIR)
    ap.add_argument("--cache-dir", default=str(DEFAULT_CACHE))
    ap.add_argument("--offline", action="store_true", help="只用缓存，不打网络")
    ap.add_argument(
        "--prefer",
        choices=("api", "raw"),
        default="api",
        help="先走哪条路。本机实测 raw 读超时、api 正常，故默认 api（但 api 吃 60 次/小时额度）",
    )
    ap.add_argument("--http-timeout", type=float, default=20.0)
    ap.add_argument("--limit", type=int, default=0, help="只取前 N 个源文件（0=全部）")
    ap.add_argument("--server", action="append", default=[], help="introspect：命令，可重复")
    ap.add_argument("--servers-from", default=None, help="introspect：从 JSON 文件读命令行列表")
    ap.add_argument("--no-default-servers", action="store_true", help="introspect：不用内置候选")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    if args.source == "schemas":
        collected = collect_schemas(
            repo=args.repo,
            subdir=args.subdir,
            cache=Path(args.cache_dir),
            offline=args.offline,
            limit=args.limit,
            prefer=args.prefer,
            http_timeout=args.http_timeout,
        )
    else:
        commands = list(args.server)
        if args.servers_from:
            loaded = json.loads(Path(args.servers_from).read_text(encoding="utf-8"))
            commands.extend(str(c) for c in loaded)
        if not commands and not args.no_default_servers:
            commands = list(DEFAULT_SERVERS)
        if not commands:
            print("introspect 模式没有给出任何 server 命令", file=sys.stderr)
            return 2
        collected = collect_introspected(commands, timeout=args.timeout)

    corpus = build_corpus(collected)
    summarize(corpus)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(corpus, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已写入 {out}（{out.stat().st_size:,} 字节）")
    # **一个工具都没抓到就是失败**：静默产出一个空语料，比报错危险得多。
    return 0 if corpus["meta"]["n_tools"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
