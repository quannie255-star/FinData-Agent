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

同一个变体的第二形态见 `misplaced_defaults`：不是"忘了写默认值"，而是
默认值写在了**别的参数**上——有值落在错误的位置，比"没有值"更难解释成疏忽。
"""

from __future__ import annotations

import json
import re
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

# description 里出现这些措辞，等于作者"口头承诺"了这个参数可以省略。
# 口头承诺与 schema 不一致时，冲突的不是 agent，是数据源自己。
_HINT_CLAIMS_VALUE = ("default", "默认值", "缺省")
_HINT_CLAIMS_OPTIONAL = ("optional", "可选", "if not provided")
_DEFAULT_HINT = _HINT_CLAIMS_VALUE + _HINT_CLAIMS_OPTIONAL

# 从 description 里抠"作者说的默认值是多少"。**只认数字**：字符串默认值
# （'all' / 'desc' / 'bitcoin'）在自然语言里满天飞，抠出来全是噪声；数字才
# 可能在 schema 里找到唯一落点，也才可能被证明"贴错了位置"。
_CLAIMED_NUMBER = re.compile(
    r"default\s*(?:is|value\s+is|=|:)?\s*([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
)


def default_claim(param: dict[str, Any]) -> str:
    """description 对"可省略"的声明的**强度**：`"value"` / `"optional"` / `""`。

    必须区分这两件事："描述里写明了默认值是多少"说明作者想过不传时用什么；
    "标了一句 optional"只说明可以不传。把后者也写成"称有默认值"，等于我替
    作者说了他没说过的话——正是本项目一直在抓的那类错误，只不过这次犯错的是
    我自己写报告的手。
    """
    desc = str(param.get("description", "")).lower()
    if any(h in desc for h in _HINT_CLAIMS_VALUE):
        return "value"
    if any(h in desc for h in _HINT_CLAIMS_OPTIONAL):
        return "optional"
    return ""


def _description_claims_default(param: dict[str, Any]) -> bool:
    """参数的 description 有没有宣称它有默认值/可省略。"""
    return default_claim(param) != ""


def claimed_default(param: dict[str, Any]) -> str | None:
    """description 声称的**数字**默认值，抠不出来返回 None。"""
    m = _CLAIMED_NUMBER.search(str(param.get("description", "")).lower())
    return m.group(1) if m else None


def misplaced_defaults(spec: dict[str, Any]) -> dict[str, tuple[str, tuple[str, ...]]]:
    """找出「default 贴错位置」的参数：值在，但贴在**别人**身上。

    返回 `{参数名: (声称的值, 实际持有该值的参数名们)}`。

    与"漏标 default"的区别是证据强度：
      · 漏标 = 描述说有默认值，schema 里**找不到这个值**（还可以用"作者忘了写"解释）
      · 错位 = 描述说 p 的默认值是 X，X **确实在 schema 里**，但挂在 q 上
        ——有值落在错误的位置，比"没有值"更难用"忘了"解释

    真实案例（xlam-60k，row 65）：`calculate_electric_field` 里
    `permitivity` 的 description 写 "default is 8.854e-12"，而 permitivity
    自己**没有** default；`charge` 和 `distance`（两个都是 `type: int`）
    **双双**挂着 `default: 8.854e-12`。8.854e-12 是真空介电常数，不可能是
    以库仑为单位的电荷量、或以米为单位的距离的默认值。

    注意这是**事实陈述+一次推断**：值确实落在别的参数上（事实）；作者本意
    是给 permitivity（推断）。所以报告里必须把两件事分开写。
    """
    params = spec.get("parameters") or {}
    out: dict[str, tuple[str, tuple[str, ...]]] = {}
    for p, meta in params.items():
        if not isinstance(meta, dict) or "default" in meta:
            continue
        want = claimed_default(meta)
        if want is None:
            continue
        holders = [
            q
            for q, other in params.items()
            if q != p
            and isinstance(other, dict)
            and "default" in other
            and _same_number(other["default"], want)
        ]
        if holders:
            out[p] = (want, tuple(holders))
    return out


def _same_number(a: Any, b: str) -> bool:
    """比数值，不直接比字符串（`8.854e-12` / `0.000000000008854` 是同一个数）。"""
    try:
        return float(str(a)) == float(b)
    except (TypeError, ValueError):
        return False


def required_params(tool: dict[str, Any]) -> list[str]:
    """必填参数 = 没有标 default 的参数。

    ⚠️ **这是我设的口径，不是数据源声明的。** 该数据集 58105 个工具里，
    带 `required` 字段的是 **0 个**——没有任何一条 schema 说过哪些参数必填。
    我拿"有没有 default"来代指"必填"，理由是可机械复核、不因人而异（凭"感觉
    这个参数该传"判断会把我的偏见写进判据）；但它终究是**代理指标**：
    default 的含义是"不传时用什么值"，严格说并不等于"可以不传"——
    真实 JSON Schema 里 default 本来就不改变 required 语义。

    这个前提错在哪要写清楚：如果哪天数据源真给了 `required: true`，
    而参数又没有 default，那**才是**货真价实的漏填，我不能识别那种矛盾
    （本批没触发，因为 required 字段一个都没有）。
    """
    params = tool.get("parameters") or {}
    return [k for k, v in params.items() if isinstance(v, dict) and "default" not in v]


def _issues_against(call: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """拿**一份**工具 schema 去核对一次调用。"""
    args = call.get("arguments") or {}
    params = spec.get("parameters") or {}
    misplaced = misplaced_defaults(spec)
    issues: list[str] = []
    for p in required_params(spec):
        if p not in args:
            if p in misplaced:
                want, holders = misplaced[p]
                issues.append(
                    f"schema contradiction: {p} 的 description 称默认值 {want}，"
                    f"但该值被标在 {' / '.join(holders)} 上（{p} 自己没标 default）"
                )
            elif default_claim(params.get(p, {})) == "value":
                issues.append(
                    f"schema contradiction: {p} 的 description 称有默认值，schema 未标 default"
                )
            elif default_claim(params.get(p, {})) == "optional":
                # 只说"可选"、没给默认值：仍然不该判 agent 漏填，但证据更弱，
                # 报出来的话就不能再说成"称有默认值"。
                issues.append(
                    f"schema contradiction: {p} 的 description 标为可选，schema 既未标 default"
                    f" 也未标 required"
                )
            else:
                issues.append(f"missing required: {p}")
    for k in args:
        if k not in params:
            issues.append(f"unknown argument: {k}")
    return issues


def inspect_call(call: dict[str, Any], tools: list[dict[str, Any]]) -> list[str]:
    """对一次调用做确定性检查，返回问题列表（空列表 = 没查出问题）。

    五类问题，**按证据强度排序**——越靠前的越不需要解释：

    1. `tool not found`      工具名不在注册表里
    2. `schema contradiction (default 错位)`
                              参数没传，description 说它的默认值是 X，
                              **而 X 确实在 schema 里**，只是挂在别的参数上
    3. `schema contradiction` 参数没传，但 description 说它有默认值
                              ——**这是数据源自相矛盾，不是 agent 漏填**
    4. `missing required`     参数没传，description 也没承诺过默认值
    5. `unknown argument`     传了 schema 里没有的参数名

    2 与 3 的区别只是证据强度，处置相同（都要动数据源，不动样本）。

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
