"""xlam-function-calling-60k 适配器：真实 tool-calling 语料 → Trace。

为什么用这个数据集
------------------
项目要的是「Agent 轨迹质检」，那就必须拿**真实轨迹**说话，不能自己造——
自己造的样本证明不了自己的规则（规则怎么写的，样本就怎么长，100% 是循环
论证）。xlam-60k 是 Salesforce 公开的 function-calling 语料，每条记录是
「一句自然语言 query + 可用工具列表 + 期望的调用序列」，正好是轨迹的形状。

关键设计：**golden 里只放可复核的事实出处，不放正确答案**。
这个数据集没有"哪条是脏的"标签，硬塞一个就等于把结论写进前提。所以
`golden = {source, row, unlabeled: True}`——它唯一的作用是让每条判定都能
回到原始记录去人工复核。**无标签质检正是生产环境的常态**：线上跑的轨迹
没人给你标哪条错了。

与 dq 层同构的地方
------------------
`inspect_call` 只查三件确定性事实（名字在不在注册表、必填项有没有、
参数名认不认），跟 `dq/probes.py` 一样"只观测不判断"。但这里观测出来的
第一类问题不是 agent 的错——是**数据源自己打架**（见 `schema_contradiction`
的注释），这正是本项目"形态相同、结论相反"在真实数据上的第一次咬人。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from findata.agentops.schema import STATUS_ERROR, STATUS_OK, Trace

DATASET = "Salesforce/xlam-function-calling-60k"
# ModelScope 可达（HuggingFace 在本机网络下不通）；--depth 1 只取最后一次提交，
# 不把 96MB 之外的历史也拖下来。
REPO_URL = "https://www.modelscope.cn/datasets/AI-ModelScope/xlam-function-calling-60k.git"
DEFAULT_RAW = Path("data/raw/xlam")
FILENAME = "xlam_function_calling_60k.json"

# description 里出现这些措辞，等于作者"口头承诺"了这个参数有默认值。
# 口头承诺与 schema 不一致时，冲突的不是 agent，是数据源自己。
_DEFAULT_HINT = ("default", "默认值", "optional", "可选", "if not provided", "缺省")


def _description_claims_default(param: dict[str, Any]) -> bool:
    """参数的 description 有没有宣称它有默认值/可省略。"""
    desc = str(param.get("description", "")).lower()
    return any(h in desc for h in _DEFAULT_HINT)


def required_params(tool: dict[str, Any]) -> list[str]:
    """必填参数 = 没有标 default 的参数。

    这是 JSON Schema 的通行口径：没给 default 就必须显式传。凭"感觉这个
    参数应该要传"来判断会因人而异，而 default 是 schema 里写着的事实。
    """
    params = tool.get("parameters") or {}
    return [k for k, v in params.items() if isinstance(v, dict) and "default" not in v]


def _issues_against(call: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """拿**一份**工具 schema 去核对一次调用。"""
    args = call.get("arguments") or {}
    params = spec.get("parameters") or {}
    issues: list[str] = []
    for p in required_params(spec):
        if p not in args:
            if _description_claims_default(params.get(p, {})):
                issues.append(
                    f"schema contradiction: {p} 的 description 称有默认值，schema 未标 default"
                )
            else:
                issues.append(f"missing required: {p}")
    for k in args:
        if k not in params:
            issues.append(f"unknown argument: {k}")
    return issues


def inspect_call(call: dict[str, Any], tools: list[dict[str, Any]]) -> list[str]:
    """对一次调用做确定性检查，返回问题列表（空列表 = 没查出问题）。

    四类问题，**按证据强度排序**——越靠前的越不需要解释：

    1. `tool not found`      工具名不在注册表里
    2. `schema contradiction` 参数没传，但 description 说它有默认值
                              ——**这是数据源自相矛盾，不是 agent 漏填**
    3. `missing required`     参数没传，description 也没承诺过默认值
    4. `unknown argument`     传了 schema 里没有的参数名

    ⚠️ 同名工具在注册表里可能出现**多次**（该数据集确实有：`tools` 里
    `time_series` / `web_search` 都重复注册了，两份 schema 参数还不一样）。
    只取第一份就核对，会把"数据源重复注册"误判成"agent 传错参数"——第一次
    跑就误报了 45 条 unknown argument。所以规则是：**任一份 schema 能解释
    这次调用就放行**，全解释不了才报（报问题最少的那一份）。
    """
    name = str(call.get("name", ""))
    cands = [t for t in tools if t.get("name") == name]
    if not cands:
        return [f"tool not found: {name}"]

    best: list[str] | None = None
    for spec in cands:
        issues = _issues_against(call, spec)
        if not issues:
            return []
        if best is None or len(issues) < len(best):
            best = issues
    return best or []


def _as_list(raw: Any) -> list[Any]:
    """数据集里 tools / answers 是 JSON **字符串**，不是已解析的数组。"""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return []
    return list(raw or [])


def to_trace(rec: dict[str, Any], idx: int) -> Trace:
    """一条原始记录 → 一条 Trace。

    同一条记录里出现**完全一样**的调用（同名 + 同参数）单独记一笔
    `duplicate call`：正常轨迹里同参数重复调用极罕见，它要么是标注重复，
    要么是 agent 在原地打转，两者都值得人看一眼。
    """
    query = str(rec.get("query", ""))
    tools = _as_list(rec.get("tools"))
    answers = _as_list(rec.get("answers"))

    t = Trace(task=query, agent="xlam-60k", parser="dataset")
    seen: set[str] = set()
    for call in answers:
        name = str(call.get("name", ""))
        args = call.get("arguments") or {}
        key = json.dumps([name, args], sort_keys=True, ensure_ascii=False, default=str)
        issues = inspect_call(call, tools)
        if key in seen:
            issues = [*issues, "duplicate call"]
        seen.add(key)

        if issues:
            t.step(name, arguments=args, status=STATUS_ERROR, error="; ".join(issues))
        else:
            t.step(name, arguments=args, status=STATUS_OK)

    t.finish(n_calls=len(answers))
    # golden 只放出处，不放结论：这批数据**没有**"哪条脏"的标签，
    # 硬塞标签等于把结论写进前提。unlabeled=True 就是把这个事实记下来。
    t.golden = {"source": DATASET, "row": idx, "unlabeled": True}
    return t


def iter_records(
    limit: int | None = None, path: str | Path | None = None
) -> Iterator[tuple[int, dict[str, Any]]]:
    """流式读原始 JSON，**不整份 load 进内存**。

    96MB 的文件 `json.load` 一下就能把开发环境撑爆（本机实测：连 echo
    都打不出来）。ijson 按 item 逐个吐，2 万条和 6 万条的内存占用一样。
    """
    import ijson  # 局部导入：只在真正读原始语料时才需要

    p = Path(path) if path else DEFAULT_RAW / FILENAME
    with p.open("rb") as f:
        for i, rec in enumerate(ijson.items(f, "item")):
            if limit is not None and i >= limit:
                break
            yield i, rec


def to_training_example(rec: dict[str, Any], idx: int) -> dict[str, Any]:
    """一条原始记录 → **可直接喂 SFT 的样本**（OpenAI messages + tool_calls 形状）。

    形状刻意用 OpenAI 那一套而不是自造：落盘即训练，中间不用再写一层转换
    （每多一层转换，就多一处口径漂移）。注意 `arguments` 在 OpenAI 协议里是
    **JSON 字符串**不是对象——这个细节错了，下游加载会整批失败。
    """
    return {
        "id": str(rec.get("id") or f"xlam-{idx}"),
        "source": DATASET,
        "row": idx,
        "messages": [
            {"role": "user", "content": str(rec.get("query", ""))},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {
                            "name": str(a.get("name", "")),
                            "arguments": json.dumps(
                                a.get("arguments") or {}, ensure_ascii=False, sort_keys=True
                            ),
                        },
                    }
                    for a in _as_list(rec.get("answers"))
                ],
            },
        ],
        "tools": _as_list(rec.get("tools")),
    }


def fetch(dest: str | Path = DEFAULT_RAW) -> Path:
    """拉原始数据。已存在就直接复用，不重复下载。"""
    d = Path(dest)
    target = d / FILENAME
    if target.exists():
        return target
    d.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", REPO_URL, str(d)], check=True)
    if not target.exists():
        raise FileNotFoundError(f"克隆完成但没找到 {target}，请检查数据集目录结构")
    return target
