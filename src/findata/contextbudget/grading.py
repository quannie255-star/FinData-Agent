"""精确值判据：把「关键词出现」换成「**必需事实**命中」。

为什么要有它（`docs/r5.0-acceptance.md` §4.5）
--------------------------------------------
`tasks.is_hit` 是弱判据：只要一个词出现就算过。7B 的输出是长篇中文散文，
"提到了这个词"和"答对了"之间差得很远；而且它对**答非所问但恰好用了那个词**
完全无感。

本模块要求**每一条必需事实都出现**，每条事实给一个**别名组**
（同义/等价的多种写法都算）。事实本身取自冻结快照 `baf6420` 的原文，
不是凭印象写的。

偏差方向（必须一起引）
--------------------
别名组是手写的，不可能穷举。所以本判据的偏差方向主要是**漏判**——
答对了但用了没列出的写法会被判不过。

但**不能说"不会把答错判成答对"**——那是错的，实测反例见下。
还有一条独立的误判路径：**宽泛词可以被跑题答案蹭到**。
例如 t22 的答案是一句拒答"没有找到与 ungrounded_slot 探针相关的**防误报**措施的
代码"，它命中了别名组里的"防误报"；t32 的答案整段在讲 `triage.py`（跑题），
却因为句中出现"该函数**依赖**于多个常量"而命中"依赖"。
⇒ 别名组里凡是没有**判别力**的词（依赖 / 防线 / 规则 / 通用 …），都可能被蹭。

还有一条更硬的路径：**判据泄漏**（见 `has_exam_leak`）。冻结语料根里放着
`src/findata/contextbudget/tasks.py`，**那份文件同时包含全部 32 道题和它们的
`must_contain`**。模型只要读它，答案里就会带上判据本身。实测 active7b 臂
t23 就是这么过判据的：整段在讲 `tasks.py`（跑题），却引用了
`Task("t23", "…缺陷种类是怎么推导出来的？…", ("原文",), …)`——
问题和判据一起被抄进了答案。

两条路径合起来的结论：`strong=True` 只说明**"必需事实出现过"**，
既不保证答对，也不保证没答错。泄漏答案以 `failed_by="leaked"` 单列并
**剔出各档统计**（不重跑：`runs-*.jsonl` 存了答案原文，事后剔除不影响两臂可比性）。

少数任务（如 t09 要求连符号一起写出来）本判据比题目实际要求更严，
这是刻意的：宁可漏判，不可误判。

与弱判据的关系：**两个都报，不合并**。弱判据用于与历史读数保持可比，
强判据用于支撑质量侧结论。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .tasks import Task, is_hit

__all__ = [
    "LEAK_FINGERPRINTS",
    "STRONG",
    "JudgeResult",
    "StrongSpec",
    "has_exam_leak",
    "judge",
    "mcnemar_exact",
]

_WS = re.compile(r"\s+")

# 泄漏指纹：只有**引用了考题定义文件本身**才会出现的东西（tasks.py / 我写的判据）。
# 故意取窄：宁可漏判"这条泄漏了"，也不要冤枉一条正常答案——
# 一个"泄漏"标记会直接把答案剔出统计，误标就是误删数据。
LEAK_FINGERPRINTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"must_contain"),
    re.compile(r'Task\(\s*"t\d\d"'),  # Task("t01", …) —— 只有定义处才有这个形状
    re.compile(r'tags=\(\s*"(?:single|multi|cross)"'),
)


def has_exam_leak(answer: str) -> bool:
    """答案是否引用了"考题定义"（= 泄漏了判据）。

    背景：审计语料的根目录就是本仓库的冻结快照，而
    `src/findata/contextbudget/tasks.py` 里写着全部题目**以及**
    每题的 `must_contain`。模型读它 = 拿到答案卡。
    """
    return any(p.search(answer) for p in LEAK_FINGERPRINTS)


def _norm(text: str) -> str:
    """归一化：去掉所有空白 + 转小写。

    去空白是刻意的：`2026-09-18.2` 与 `2026-09-18 . 2`、`test_docs_consistency.py`
    与折行断开都会影响朴素 `in` 判断，而折行是模型输出的常态。
    """
    return _WS.sub("", text).lower()


@dataclass(frozen=True)
class StrongSpec:
    """一组**必需事实**：每个 group 至少要命中一种别名写法。"""

    groups: tuple[tuple[str, ...], ...]
    note: str = ""


# 事实来源：冻结快照 baf6420 的原文（见 docs/r5.0-acceptance.md §5 的冻结步骤）
STRONG: dict[str, StrongSpec] = {
    "t01": StrongSpec((("xlam.py",), ("triage.py",)), "agentops/ 下确实有这两个 py"),
    "t02": StrongSpec((("metrics.py",), ("runner.py",)), "eval/ 下确实有这两个"),
    "t03": StrongSpec((("roadmap.md",), ("badge.md",)), "docs/ 下确实有这两个"),
    "t04": StrongSpec((("run_trust_filter.py",), ("daily_pipeline.py",)), "scripts/ 确有两个"),
    "t05": StrongSpec((("test_docs_consistency.py",), ("test_agentops.py",)), "tests/ 确有两个"),
    "t06": StrongSpec((("triage.py",), ("report.py",)), "agent/nodes 确有两个"),
    "t07": StrongSpec((("telemetry.py",), ("attribution.py",)), "contextbudget 确有两个"),
    "t08": StrongSpec((("2026-09-18",),), "RULES_VERSION = 2026-09-18.2"),
    "t09": StrongSpec(
        (
            ("✓", "正样本"),
            ("✗", "丢弃"),
            ("⚠", "需人工"),
            ("⊘", "不算失败"),
            ("🔧", "待修"),
        ),
        "五档：符号与名称都要（比原题更严，宁漏判）",
    ),
    "t10": StrongSpec((("待修",), ("可逆", "保留", "改数据源", "修 schema")),
                      "第五档 🔧 待修 schema + 动机是样本可逆/改数据源"),
    "t11": StrongSpec((("错位",), ("default",)), "DEFECT_MISPLACED = 'default 错位'"),
    "t12": StrongSpec((("ok",), ("loud",), ("silent",)), "SEV_OK/SEV_LOUD/SEV_SILENT"),
    "t13": StrongSpec((("蒙",), ("路径",)), "标签 '蒙对·结果对但路径脏'"),
    "t14": StrongSpec(
        (("待修",), ("数据源", "schema"), ("agent",)), "V_SCHEMA_DEFECT，且必须说清不是 agent 的错"
    ),
    "t15": StrongSpec((("inmemory",), ("exporter",)), "in_memory 挂 InMemory exporter"),
    "t16": StrongSpec(
        (("none",), ("初始化", "setup", "未配置", "没配")), "未 setup 时返回 None"
    ),
    "t17": StrongSpec((("pii",),), "docstring 里明确写了 PII"),
    "t18": StrongSpec((("未观测",), ("诚实", "不代表", "0")), "retry 未观测，不许当 0 结论"),
    "t19": StrongSpec((("mcp",), ("沉默", "用不到", "用不上")), "沉默工具的存在理由是 MCP 生态"),
    "t20": StrongSpec((("get_signature",), ("对照",)), "tools.py 里的一组对照"),
    "t21": StrongSpec((("进程",), ("容器",)), "进程级隔离；容器级本机做不了"),
    "t22": StrongSpec((("symbol",), ("防线", "误报")), "三道防线，symbol 是其中一条"),
    "t23": StrongSpec((("原文",), ("推导", "派生", "规则")), "从原文推导，不新增字段"),
    "t24": StrongSpec((("探测",), ("铁律", "同一", "共享")), "两个 triage 共享「探针只观测」"),
    "t25": StrongSpec(
        (("agent",), ("训练", "轨迹", "质检"), ("领域包", "a股", "a 股", "不再是主线")),
        "快照 README：主线 = Agent 轨迹质检(v3.0)；A股降为领域包",
    ),
    "t26": StrongSpec((("findata",), ("2.2.0",)), "pyproject: name=findata, version=2.2.0"),
    "t27": StrongSpec(
        (("langgraph",), ("opentelemetry", "otel", "trace")), "编排 + trace 两类依赖"
    ),
    "t28": StrongSpec(
        (("collect-only",), ("test_docs_consistency", "文档")), "核心判据是跑一次 collect-only"
    ),
    "t29": StrongSpec((("agent",), ("训练", "轨迹", "质检")), "AGENTS.md 当前主赛道"),
    "t30": StrongSpec((("v4.0",), ("上下文", "策展")), "ROADMAP 顶部 = v4.0 上下文策展"),
    "t31": StrongSpec(
        (("findata_",), ("metric_query", "ask", "trust_check", "trust_board"),
         ("health_check", "dashboard", "validate", "list_metrics", "execute_metric")),
        "9 个工具；两个不交的家族各至少一个 ⇒ 至少举出 2 个具体名字",
    ),
    "t32": StrongSpec((("通用",), ("依赖", "被依赖", "独立")), "generic 不依赖 domains/finance"),
}


@dataclass(frozen=True)
class JudgeResult:
    task_id: str
    weak: bool
    strong: bool
    failed_by: str  # "" | "empty" | "leaked" | "no_lookup" | "missing_fact"
    missing: tuple[str, ...]
    leaked: bool = False

    @property
    def strong_verified(self) -> bool:
        """强判据且过程成立 —— 目前可对外说的那个数。"""
        return self.strong


def judge(task: Task, answer: str, *, n_tool_calls: int, stopped_reason: str) -> JudgeResult:
    """两把尺一起给：弱（关键词）+ 强（必需事实 + 过程）。

    任一档都**不是**"答对"，见模块 docstring 的两条误判路径。
    """
    spec = STRONG.get(task.id)
    weak = bool(is_hit(task, answer))
    if not answer.strip():
        return JudgeResult(task.id, weak, False, "empty", ())
    if has_exam_leak(answer):
        # 泄漏优先于其它判据：这条答案"命中的是判据本身"，不构成任何证据。
        # 单列成 `leaked` 而不是混进 `missing_fact`，否则会把
        # "语料里放着答案卡"这个真问题伪装成"模型答错了"。
        return JudgeResult(task.id, weak, False, "leaked", (), leaked=True)
    if n_tool_calls <= 0 or stopped_reason != "answered":
        return JudgeResult(task.id, weak, False, "no_lookup", ())
    if spec is None:
        # 没有强判据定义的题**不许**默认通过：那会把"没写判据"变成"答对了"
        return JudgeResult(task.id, weak, False, "missing_fact", ("<未定义强判据>",))
    blob = _norm(answer)
    missing = tuple(
        "/".join(group) for group in spec.groups if not any(_norm(alt) in blob for alt in group)
    )
    return JudgeResult(task.id, weak, not missing, "" if not missing else "missing_fact", missing)


def mcnemar_exact(only_a: int, only_b: int) -> tuple[float, float, float]:
    """配对二值对照的 McNemar 精确检验。返回`（双侧 p, 单侧 p, 分辨力下限）`。

    三个数缺一不可，因为它们回答的是不同问题：

    - 双侧 p：差异能否排除偶然。
    - 单侧 p：**"B 不差"**这个方向性假设的 p。本项目本来就是方向性假设，
      只报双侧等于把该问的问题漏掉；但只报单侧就是偷偷降门槛，所以两个都报。
    - **分辨力下限** = k 对不一致时，最极端同向能取到的**最小**双侧 p（`2^(1-k)`）。
      这是本函数最有用的输出：它只由 k 决定，**与两臂实际差多少无关**。
      k=4 ⇒ 0.125，k=5 ⇒ 0.0625 ⇒ **k<6 时任何结果都不可能显著**。
      ⇒ 报"不显著"时若不说分辨力，读者会读成"差不多"；
      真相是"本对照没有能力分辨"，两句话结论完全不同。
    """
    from math import comb

    k = only_a + only_b
    if k <= 0:
        return 1.0, 1.0, 1.0
    tail = min(only_a, only_b)
    two_sided = min(1.0, 2 * sum(comb(k, i) for i in range(tail + 1)) / 2**k)
    one_sided = sum(comb(k, i) for i in range(only_b + 1)) / 2**k
    return two_sided, one_sided, 2.0 ** (1 - k)
