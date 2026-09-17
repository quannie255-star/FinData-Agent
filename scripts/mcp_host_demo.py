r"""通用外部 MCP 宿主演示（R2 验收：真实外部宿主集成）。

这是一个**对 findata 内部一无所知**的通用 Agent 宿主：只讲标准 MCP 协议
（stdio JSON-RPC），通过 `findata-mcp` 命令接入，运行时发现工具、按
JSON Schema 传参——没有任何一行 import findata。它演示的正是
「findata 作为可信增强模块挂到成熟 Agent 项目上」的最小接入形态：
换掉本文件的 LLM 或编排逻辑，接入纪律不变。

集成契约（docs/integration.md）：
    1. 会话开始调 trust_board 拉全板；
    2. 引用任何数字前查 trust_check，usable=false 不得作为已核验引用；
    3. 未知指标错误带可用键，据此自愈而不是放弃检查。

用法：
    uv run python scripts/mcp_host_demo.py                 # 真实 LLM（需 FINDATA_LLM_API_KEY）
    uv run python scripts/mcp_host_demo.py --no-llm        # 脚本模式（零 LLM，CI 可跑）
    uv run python scripts/mcp_host_demo.py --question "..." # 自定义问题

退出码：0 全过；1 失败（协议错误 / 门禁失败）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# ─────────────────────────── MCP 客户端（标准协议层）───────────────────────────

from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def _server_params() -> StdioServerParameters:
    """宿主眼里的 findata 就是一条启动命令——配置级接入，零协议开发。

    PYTHONPATH 兜底：干净环境没 pip install 项目时也能拉起 server 子进程。
    """
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "findata.mcp.server"],
        cwd=str(ROOT),
        env={**os.environ, "PYTHONPATH": str(ROOT)},
    )


def _load_env_file(path: Path) -> None:
    """通用宿主读自己的配置文件（KEY=VALUE），不覆盖已存在的环境变量。

    宿主不借道 findata 的 settings——那是被增强方的内部实现。
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'").strip('"'))


_load_env_file(ROOT / ".env")


def _parse_result(res: Any) -> Any:
    """MCP CallToolResult → Python 对象（取 text 内容块，能 JSON 就 JSON）。"""
    texts = [getattr(c, "text", "") for c in (res.content or [])]
    raw = "\n".join(t for t in texts if t)
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# ─────────────────────────── LLM 工具环（通用宿主逻辑）───────────────────────────

SYSTEM_PROMPT = """你是接入了 findata MCP 工具的通用数据分析 Agent。回答数据类问题时必须遵守：
1. 数字用 findata_metric_query 查，不许编造；
2. 引用任何数字前，先对它所属的 table.metric 调 findata_trust_check：
   usable=true 才能作为已核验引用；badge=caution 只能以"仅借鉴"表述并转述疑点；
   badge=unusable 拒绝引用并说明根因；
3. 最终答案里每个数据数字后面用〔徽章文案｜table.metric〕标注可信度；
4. 用简洁中文回答。"""

_MAX_ROUNDS = 5


async def llm_answer(
    session: ClientSession, tools: list[Any], question: str
) -> tuple[str, list[dict]]:
    """通用工具环：LLM 只负责选工具与写作，数字与徽章全部来自 MCP 返回。"""
    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_openai import ChatOpenAI

    specs = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.input_schema or {"type": "object"},
            },
        }
        for t in tools
    ]
    llm = ChatOpenAI(
        model=os.environ.get("FINDATA_LLM_MODEL", "deepseek-chat"),
        base_url=os.environ.get("FINDATA_LLM_BASE_URL", "https://api.deepseek.com"),
        api_key=os.environ.get("FINDATA_LLM_API_KEY", ""),
        temperature=0.0,
        timeout=60,
    ).bind_tools(specs)

    messages: list = [HumanMessage(content=question)]
    checks: list[dict] = []
    for _ in range(_MAX_ROUNDS):
        ai = llm.invoke(messages)
        messages.append(ai)
        calls = getattr(ai, "tool_calls", None) or []
        if not calls:
            return str(ai.content), checks
        for call in calls:
            res = await session.call_tool(call["name"], call["args"])
            payload = _parse_result(res)
            if call["name"] == "findata_trust_check" and isinstance(payload, dict):
                checks.append(payload)
            messages.append(
                ToolMessage(
                    content=json.dumps(payload, ensure_ascii=False), tool_call_id=call["id"]
                )
            )
    raise RuntimeError(f"工具环超过 {_MAX_ROUNDS} 轮未收敛")


async def scripted_answer(session: ClientSession, tools: list[Any]) -> tuple[str, list[dict]]:
    """脚本模式：不用 LLM，按集成契约走一遍固定调用序列（证据全部真实）。"""
    board = _parse_result(await session.call_tool("findata_trust_board", {"source": "synthetic"}))
    check = _parse_result(
        await session.call_tool(
            "findata_trust_check",
            {"table": "stock_daily", "metric": "close", "source": "synthetic"},
        )
    )
    checks = [board, check]
    if check.get("usable"):
        answer = (
            f"演示问题：当前数据可信度如何？\n"
            f"健康分 {board['health_score']}（{board['grade']}），"
            f"全板 {len(board['badges'])} 项徽章。"
            f"stock_daily.close 当前为〔{check['mark']}〕，可以引用；"
            f"证据示例：{check['evidence'][0][0] if check['evidence'] else '（通用基线检查）'}。"
        )
    else:
        answer = (
            f"stock_daily.close 当前为〔{check['mark']}〕，"
            f"不得作为已核验引用：{check['summary']}"
        )
    return answer, checks


async def main_async(use_llm: bool, question: str | None) -> list[str]:
    out: list[str] = []

    def emit(line: str = "") -> None:
        out.append(line)
        print(line)

    params = _server_params()
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            emit("══ 1. 协议握手（stdio JSON-RPC，进程隔离）══")
            emit(
                f"server: {info.server_info.name} v{info.server_info.version}"
                f" · 协议版本 {info.protocol_version}"
            )
            emit("")
            tools = (await session.list_tools()).tools
            emit("══ 2. 工具发现（运行时按 MCP 列出，宿主零硬编码）══")
            for t in tools:
                emit(f"  - {t.name}: {(t.description or '').splitlines()[0][:52]}…")
            emit(f"共 {len(tools)} 个工具")
            emit("")

            emit("══ 3. 提问 → 工具环 → 带徽章引用的回答 ══")
            q = question or "贵州茅台（600519）最新收盘价是多少？这个数字可以放心引用吗？"
            emit(f"问题: {q}")
            emit("")
            if use_llm:
                answer, checks = await llm_answer(session, tools, q)
                mode = "真实 LLM（外部宿主自己的模型，与 findata 无关）"
            else:
                answer, checks = await scripted_answer(session, tools)
                mode = "脚本模式（零 LLM）"
            emit(f"模式: {mode}")
            emit("")
            emit("── 宿主收到的可信度证据（trust_check / trust_board 原始返回）──")
            emit(json.dumps(checks, ensure_ascii=False, indent=2)[:2000])
            emit("")
            emit("── 最终回答（逐数字带徽章）──")
            emit(answer)
            emit("")

            # 宿主侧门禁复核：答案引用的检查里不许有 usable=false 被当已核验
            emit("══ 4. 宿主侧契约复核 ══")
            bad = [c for c in checks if isinstance(c, dict) and c.get("usable") is False]
            emit(f"trust 检查 {len(checks)} 次，其中 usable=false {len(bad)} 次"
                 + ("（须以疑点/拒绝口径表述，不得作为已核验引用）" if bad else ""))
            emit("")
            emit("结论：外部宿主经标准 MCP 协议完成 发现→调用→带徽章引用 全流程，"
                 "零 findata 内部依赖、零协议开发。")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="通用外部 MCP 宿主 × findata 可信增强模块")
    parser.add_argument("--no-llm", action="store_true", help="脚本模式（零 LLM）")
    parser.add_argument("--question", type=str, default=None, help="自定义问题")
    parser.add_argument("--out", type=str, default=None, help="transcript 落盘路径")
    args = parser.parse_args()

    lines = asyncio.run(main_async(use_llm=not args.no_llm, question=args.question))
    if args.out:
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"[transcript] -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
