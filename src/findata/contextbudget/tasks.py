"""任务集：32 个**真实**的仓库问答任务。

**为什么用仓库问答**：它天然需要"读源码"这个动作，而读源码正是调研里
token 消耗的绝对大头——一份对编码 agent 轨迹的分析发现，读操作
（cat / grep / head 这类文件与目录检查）占了 token 消耗的 **76.1%**
（SWE-Bench Verified 轨迹分析，转述）。用这个场景，复现的就是真实分布，
不是我挑出来的极端例子。

**关于 `must_contain`：这是弱判据，别当准确率用。**
它的作用是"答案里有没有出现该出现的那个东西"，用来给一次运行一个
**确定性的**、可在 CI 里断言的标签。它**判不出**：
  - 答案是否只是恰好提到了这个词（说的是对的还是错的词）
  - 推理过程是否成立
所以报告里引用它时必须写"关键词命中"，不许写成"回答正确"。
真正的成功率对照要等 R5.2（那时才有改前/改后两臂可比）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["TASKS", "Task", "is_hit"]


@dataclass(frozen=True)
class Task:
    id: str
    question: str
    must_contain: tuple[str, ...]
    note: str = ""
    tags: tuple[str, ...] = field(default_factory=tuple)


# 难度分三档（tags 里的 single / multi / cross）：
#   single = 一次 read_file 通常就够
#   multi  = 需要先定位再读，或需要理解某段逻辑
#   cross  = 跨文件/跨目录，通常要多次调用
TASKS: tuple[Task, ...] = (
    # --- 清单类：诱发 list_files ---
    Task("t01", "agentops 目录下有哪些 Python 文件？", ("xlam.py",), tags=("single",)),
    Task("t02", "eval 目录下有哪些 Python 模块？逐个列出。", ("metrics.py",), tags=("single",)),
    Task("t03", "docs 目录下有哪些 markdown 文档？逐个列出。", ("ROADMAP.md",), tags=("single",)),
    Task("t04", "scripts 目录下有哪些脚本？", ("run_trust_filter.py",), tags=("single",)),
    Task("t05", "tests 目录下有哪些测试文件？", ("test_docs_consistency.py",), tags=("single",)),
    Task("t06", "agent/nodes 目录下有哪些节点文件？", ("triage.py",), tags=("single",)),
    Task("t07", "contextbudget 子包里有哪些模块？", ("telemetry.py",), tags=("single",)),
    # --- 事实类：诱发 search_code / read_file ---
    Task(
        "t08",
        "agentops 的 RULES_VERSION 常量现在的值是多少？",
        ("2026-09-18",),
        note="版本号是判定逻辑的版本锚点，改判定必须改它",
        tags=("single",),
    ),
    Task(
        "t09",
        "agentops 里一共有哪几档处置？把每一档的符号和名称都写出来。",
        ("正样本", "丢弃"),
        tags=("multi",),
    ),
    Task(
        "t10",
        "五档处置里的第五档是什么？当初为什么要新增它？",
        ("待修",),
        note="答案在 triage.py 的注释里解释了动机",
        tags=("multi",),
    ),
    Task(
        "t11",
        "export.py 里 DEFECT_MISPLACED 这个常量的值是什么？中文原文。",
        ("错位",),
        tags=("single",),
    ),
    Task(
        "t12",
        "triage.py 里严重度（severity）有哪三档？各自是什么含义？",
        ("loud", "silent"),
        tags=("multi",),
    ),
    Task(
        "t13",
        "CAT_LUCKY_GUESS 这个分类代表什么情况？请解释并给出它的中文标签。",
        ("蒙",),
        note="中文标签里含「蒙对」二字",
        tags=("multi",),
    ),
    Task(
        "t14",
        "triage.py 里 CAT_SCHEMA_CONTRADICTION 应该被判成哪一档处置？为什么不是丢弃？",
        ("待修",),
        tags=("multi",),
    ),
    # --- 机制类：需要理解一段逻辑 ---
    Task(
        "t15",
        "observability 的 setup_tracer 里 in_memory 参数的作用是什么？它挂载的是哪个 exporter？",
        ("InMemory",),
        tags=("multi",),
    ),
    Task(
        "t16",
        "observability 里的 get_in_memory_exporter 在什么情况下返回 None？这么设计是为了什么？",
        ("none",),
        note="判据用 lower 比对，None / none 都算",
        tags=("multi",),
    ),
    Task(
        "t17",
        "contextbudget 的 telemetry.py 里，为什么用 args_hash 而不是把参数原文写进 trace？",
        ("PII",),
        note="docstring 里明确写了 PII 的理由",
        tags=("multi",),
    ),
    Task(
        "t18",
        "contextbudget 的 agent.py 里 retry_count 为什么一律记 0？这么做在诚实性上意味着什么？",
        ("未观测",),
        tags=("multi",),
    ),
    Task(
        "t19",
        "contextbudget 的 schemas.py 里什么是「沉默工具」？为什么工具清单里要有用不到的工具？",
        ("MCP",),
        tags=("multi",),
    ),
    Task(
        "t20",
        "contextbudget 的 tools.py 里，哪几个工具构成了一组对照？对照的目的是什么？",
        ("get_signature",),
        tags=("multi",),
    ),
    Task(
        "t21",
        "agentops 的 sandbox.py 是做什么的？它的隔离级别是什么，为什么不做容器级？",
        ("进程",),
        tags=("multi",),
    ),
    Task(
        "t22",
        "agentops 的 probes.py 里 ungrounded_slot 探针有哪几道防误报的防线？",
        ("symbol",),
        tags=("multi",),
    ),
    # --- 跨文件类：通常要多次调用 ---
    Task(
        "t23",
        "agentops 的 export.py 里，缺陷种类是怎么推导出来的？为什么不新增一个字段传进去？",
        ("原文",),
        tags=("cross",),
    ),
    Task(
        "t24",
        "agentops/triage.py 和 dq/triage.py 分别服务什么？它们共享哪条设计铁律？",
        ("探测",),
        tags=("cross",),
    ),
    Task(
        "t25",
        "README 里描述的当前主线是什么？它和早期的 A 股方向是什么关系？",
        ("Agent",),
        tags=("cross",),
    ),
    Task(
        "t26",
        "pyproject.toml 里项目的名字和版本号分别是多少？",
        ("findata",),
        tags=("single",),
    ),
    Task(
        "t27",
        "pyproject.toml 里列出的运行时依赖中，哪几个是与 Agent 编排和 trace 相关的？",
        ("langgraph",),
        tags=("multi",),
    ),
    Task(
        "t28",
        "tests 目录里，哪个测试文件负责校验文档里的数字与实际一致？它的核心判据是什么？",
        ("collect-only",),
        tags=("cross",),
    ),
    Task(
        "t29",
        "AGENTS.md 里记录的当前主赛道是什么？",
        ("Agent",),
        tags=("single",),
    ),
    Task(
        "t30",
        "docs/ROADMAP.md 顶部最新的一节是哪个版本？它的主题是什么？",
        ("v4.0",),
        tags=("multi",),
    ),
    Task(
        "t31",
        "mcp/server.py 通过 MCP 暴露了哪些能力？大概几个工具？",
        ("findata_",),
        tags=("multi",),
    ),
    Task(
        "t32",
        "generic 子包和 domains/finance 是什么关系？谁依赖谁？",
        ("通用",),
        tags=("cross",),
    ),
)


def is_hit(task: Task, answer: str) -> bool:
    """弱判据：答案里是否出现了所有 must_contain（大小写不敏感）。

    再次强调：命中 ≠ 答对。报告里必须写「关键词命中」。
    """
    if not answer:
        return False
    lowered = answer.lower()
    return all(token.lower() in lowered for token in task.must_contain)
