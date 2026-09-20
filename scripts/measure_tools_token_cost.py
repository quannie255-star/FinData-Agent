"""直接测「工具定义本身占多少 prompt token」——**不靠字符数换算**。

为什么需要这个脚本
------------------
`run_context_audit.py` 的账单里，工具定义的成本是用**字符数**记的
（11,559 字符/次）。字符数确定、可复现、可进断言，但它是**量级参考**，
不是 token 占比 —— 系数随语言、JSON 结构、模型分词器变化，拿一个系数去
乘就是伪精确。我在报告里明确标注了这一点，现在把这个缺口补上。

做法：**同一条消息 payload 发两次，唯一变量是有没有 tools。**
    with_tools  ->  prompt_tokens_with
    no_tools    ->  prompt_tokens_without
差值 = 工具定义吃掉的 token 数。这是**实测**，不是换算。

上游本来就在数 token（`usage.prompt_tokens`），我们直接问它要就行。

顺带得到两条曲线
----------------
1. **工具数 → token 成本**：0 / 4 / 27 个工具三档。用来回答「工具膨胀
   在 prompt 侧到底有多贵」，而不是只报一个静态占比。
2. **同一 payload 的重复一致性**：同一 payload 连发 3 次，`prompt_tokens`
   必须**逐次完全相等**。不等就说明这个量不可复现，那后面所有「省了 X%」
   都站不住。这条是自检，不是装饰。

诚实边界
--------
- 本机跑的是 7B 本地模型，**token 数由上下文结构决定、与模型无关**，
  所以这一侧的结论可迁移；但换模型/换分词器时绝对值会变（同一条文本的
  token 数本来就依赖分词器），迁移的是**占比结构**不是绝对数。
- 这里测的是**本项目的工具定义文本**。换成别的工具集，占比会变。

用法
----
    uv run python scripts/measure_tools_token_cost.py
    uv run python scripts/measure_tools_token_cost.py --repeats 5 --model qwen2.5:latest
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from findata.contextbudget.llm import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    ChatClient,
)
from findata.contextbudget.schemas import ACTIVE_TOOLS, ALL_TOOLS  # noqa: E402

# 固定的探测消息：必须**不含 tool 调用意图**，否则模型行为会掺进变量。
PROBE_MESSAGE = "请用一个词回答：Python 这门语言的创造者是谁？"

ARMS: list[tuple[str, list[dict]]] = [
    ("无工具", []),
    (f"活跃 {len(ACTIVE_TOOLS)} 个", ACTIVE_TOOLS),
    (f"全量 {len(ALL_TOOLS)} 个", ALL_TOOLS),
]


def _prompt_tokens(client: ChatClient, tools: list[dict], *, model: str) -> int:
    """发一次请求，只要 prompt_tokens。

    工具调用与否不影响我们要的量（prompt_tokens 覆盖整个输入），所以
    不强制 tool_choice —— 少一个兼容性变量。Ollama 的 OpenAI 兼容层对
    tool_choice 的支持各家不一，能不用就不用。
    """
    reply = client.complete(
        [{"role": "user", "content": PROBE_MESSAGE}],
        tools=tools or None,
        temperature=0.0,
    )
    # LLMReply.prompt_tokens 缺字段时落 0。真实 prompt 不可能 0 token，
    # 所以 0 就是"上游没给这个量"——这时不能拿估数顶上，直接失败。
    if not reply.prompt_tokens:
        raise RuntimeError("上游没回 usage.prompt_tokens —— 这个量测不了就不能估")
    return int(reply.prompt_tokens)


def main() -> int:
    ap = argparse.ArgumentParser(description="实测工具定义的 prompt token 成本")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--base-url", default=DEFAULT_BASE_URL)
    ap.add_argument("--repeats", type=int, default=3, help="每个臂重复次数（用于一致性自检）")
    ap.add_argument("--out", default=None, help="结果落盘路径（.json）")
    args = ap.parse_args()

    client = ChatClient(base_url=args.base_url, model=args.model)
    print(f"模型={args.model}  端点={args.base_url}")
    print(f"探测消息：{PROBE_MESSAGE}")
    print(f"每臂重复 {args.repeats} 次\n")

    results: dict[str, dict] = {}
    for label, tools in ARMS:
        counts: list[int] = []
        for _ in range(args.repeats):
            counts.append(_prompt_tokens(client, tools, model=args.model))
        identical = len(set(counts)) == 1
        results[label] = {
            "n_tools": len(tools),
            "repeats": counts,
            "prompt_tokens": counts[0],
            "repeat_identical": identical,
        }
        flag = "一致" if identical else f"**不一致 {counts}**"
        print(f"  {label:<16} prompt_tokens={counts[0]:>6}  重复 {args.repeats} 次 {flag}")

    base = results["无工具"]["prompt_tokens"]
    print("\n相对「无工具」基线的增量：")
    for label, entry in list(results.items())[1:]:
        delta = entry["prompt_tokens"] - base
        share = delta / entry["prompt_tokens"] * 100 if entry["prompt_tokens"] else 0.0
        entry["delta_vs_no_tools"] = delta
        entry["share_of_prompt_pct"] = round(share, 2)
        entry["delta_per_tool"] = round(delta / entry["n_tools"], 1) if entry["n_tools"] else 0.0
        print(
            f"  {label:<16} +{delta:>6} token"
            f"  占本次 prompt {share:5.1f}%"
            f"  均摊 {entry['delta_per_tool']:>6.1f} token/工具"
        )

    act = results[f"活跃 {len(ACTIVE_TOOLS)} 个"]
    full = results[f"全量 {len(ALL_TOOLS)} 个"]
    extra = full["prompt_tokens"] - act["prompt_tokens"]
    marginal = extra / (len(ALL_TOOLS) - len(ACTIVE_TOOLS))
    print(
        f"\n[核心数字] 从 {len(ACTIVE_TOOLS)} 个工具换成 {len(ALL_TOOLS)} 个工具，"
        f"仅在工具定义上多花 **{extra} token/次** 的 prompt"
        f"（边际 {marginal:.1f} token/工具）。"
    )

    consistent = all(e["repeat_identical"] for e in results.values())
    verdict = "全部通过" if consistent else "**有不一致，本批数字不可复现**"
    print(f"\n[自检] 同 payload 重复一致性：{verdict}")

    # 这条必须自己先说：否则 92.7% / 98.0% 会被当成头条数字流传出去。
    print("\n[读法警告] 「占比」不是可迁移的那个量，「绝对增量」才是。")
    print(f"  · 本脚本其余部分只有 {base} token（一句话探测消息），"
          f"所以工具定义占比看起来接近 100% ——")
    print("    这是**短 prompt 的产物**，不是工具定义的一般性质。")
    print("  · 对话越长、工具返回越多，同一批工具定义的占比就越低。")
    print(f"  · 可迁移的说法是：**这批工具定义固定吃 {full['delta_vs_no_tools']} token/次**，"
          "每轮都重发、与任务无关。")
    print("    要谈占比，必须同时给出「其余上下文有多大」，否则等于没给分母。")

    print("\n诚实说明：")
    print("  · 这是**实测**（读上游 usage.prompt_tokens），不是字符数换算")
    print("  · token 数由上下文结构决定、与模型无关 → 绝对增量可迁移；")
    print("    但同一段文本的 token 数依赖分词器，换模型会变")
    print("  · 测的是**本项目这 27 个工具定义**的文本，换工具集占比会变")
    print("  · 本机是 7B 本地模型：本脚本只测 token，不涉及成功率，")
    print("    所以这条数字**不受 7B 限制影响**")

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "model": args.model,
            "base_url": args.base_url,
            "probe_message": PROBE_MESSAGE,
            "repeats": args.repeats,
            "measured": results,
            "quantities": {
                "portable": (
                    "工具定义固定吃掉的 prompt token 绝对增量"
                    "（每轮重发、与任务无关）—— 由上下文结构决定，与模型无关"
                ),
                "scenario_specific": (
                    "占比 —— 分母是本次 prompt 总长；本脚本探测消息只有一句话，"
                    "占比因此接近 100%，是短 prompt 的产物，不可当一般结论引用"
                ),
            },
            "note": (
                "实测口径：读上游 usage.prompt_tokens，非字符数换算。"
                "引用时给绝对增量；要谈占比必须同时给出对话其余部分有多大，"
                f"否则等于没给分母。本次探测基线（无工具）仅 {base} token。"
            ),
        }
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n已写入 {out}")

    return 0 if consistent else 1


if __name__ == "__main__":
    raise SystemExit(main())
