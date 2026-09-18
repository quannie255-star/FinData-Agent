"""合成「根因已知」的失败轨迹。

**为什么必须自己造**：真实轨迹的根因是人肉复盘出来的，既无法规模化，也
算不出准确率——没有 ground truth 的评测，指标全是自说自话。合成样本的
根因是**写进去的**，所以能反过来问归因器：你判对了吗。

**合成器自己也要能被证伪**（见 `scripts/run_synth_eval.py` 的证伪检查）：
注入的失败必须在轨迹里留下**可观测信号**，否则这条合成是无效的——
要么探针瞎了，要么注入没生效。合成数据最危险的失败模式就是"看起来有
一堆样本，其实什么都没注进去"。

七类（含一类正样本对照）：
  clean        路径干净、结果正确          → 正样本
  param_error  工具参数写错                → 丢弃（能力问题）
  tool_missing 调了不存在的工具            → 不算失败（宿主配置问题）
  env_timeout  沙箱超时                    → 不算失败（环境问题）
  retry_loop   同一个工具反复调用          → 丢弃
  hallucinated 编了个用户没给的日期        → 丢弃（最隐蔽）
  lucky_guess  结果对了但路径走了兜底      → 需人工（蒙对）

确定性：给定 seed 完全可复现，别人跑出来一模一样。
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterator

from findata.agentops.schema import STATUS_DEGRADED, STATUS_ERROR, Trace
from findata.agentops.triage import (
    CAT_ENV_TIMEOUT,
    CAT_HALLUCINATED,
    CAT_LUCKY_GUESS,
    CAT_OK,
    CAT_PARAM_ERROR,
    CAT_RETRY_LOOP,
    CAT_TOOL_MISSING,
)

# 直接用 triage 的类别常量当根因名，不另起一套短名——
# 两套命名必然会漂移，漂移了评测就会把"判对了"记成"判错了"。
CLASSES = (
    CAT_OK,
    CAT_PARAM_ERROR,
    CAT_TOOL_MISSING,
    CAT_ENV_TIMEOUT,
    CAT_RETRY_LOOP,
    CAT_HALLUCINATED,
    CAT_LUCKY_GUESS,
)

_SYMBOLS = ("688981", "601088", "600030", "600519", "600900")
_NAMES = {
    "688981": "中芯国际",
    "601088": "中国神华",
    "600030": "中信证券",
    "600519": "贵州茅台",
    "600900": "长江电力",
}


def _task_price(rng: random.Random) -> tuple[str, str]:
    sym = rng.choice(_SYMBOLS)
    task = f"{_NAMES[sym]} 2025 年 {rng.randint(3, 11)} 月 {rng.randint(5, 25)} 日的收盘价是多少？"
    return sym, task


def _clean(rng: random.Random) -> Trace:
    sym, task = _task_price(rng)
    t = Trace(task=task, parser="rule")
    t.step("parse_query", arguments={"question": task}, result={"symbol": sym, "intent": "price"})
    t.step("build_sql", result={"sql": f"SELECT close FROM stock_daily WHERE symbol='{sym}'"})
    t.step("execute_sql", result={"value": round(rng.uniform(10, 200), 2)})
    t.step("trust_check", result={"badge": "VERIFIED", "usable": True})
    t.finish(final_correct=True)
    return t


# 改写池：同一类失败的不同说法。**归因器没见过这些措辞**。
# 存在的理由：如果只用 canonical 报错文本造样本，准确率必然 100%——
# 那是拿规则的原话去考规则，循环论证，数字毫无意义。改写集才测得出
# 泛化能力（以及关键词匹配的脆不脆）。
_PARAPHRASE = {
    CAT_TOOL_MISSING: [
        "ToolNotFoundError: stock_news_search",
        "no such tool: stock_news_search",
        "找不到工具：stock_news_search",
        "unknown tool 'stock_news_search'",
    ],
    CAT_ENV_TIMEOUT: [
        "TimeoutError: execution exceeded 30s",
        "timed out after 30s",
        "沙箱执行超时（30s）",
        "ExecutionTimeout: sandbox killed",
    ],
    CAT_PARAM_ERROR: [
        "bad argument: date=2025-02-30",
        "参数不合法：date=2025-02-30",
        "ValueError: invalid date field format: '2025-02-30'",
        "argument out of range: month must be 1-12",
    ],
}

_CANONICAL = {
    CAT_TOOL_MISSING: "tool not found: stock_news_search",
    CAT_ENV_TIMEOUT: "sandbox timeout after 30s",
    CAT_PARAM_ERROR: "invalid param: date=2025-02-30（2 月没有 30 日）",
}


# Held-out 措辞：**写完就不再改任何归因规则**。dev 集（_PARAPHRASE）已经
# 被用来调关键词了，在它上面拿满分是调出来的，不是泛化。这一组才回答
# "换个没见过的说法还行不行"——大概率会掉，掉了才说明测得有价值。
_HELD_OUT = {
    CAT_TOOL_MISSING: [
        "the requested function is not registered: stock_news_search",
        "没有这个工具（stock_news_search）",
    ],
    CAT_ENV_TIMEOUT: [
        "execution aborted: deadline exceeded",
        "超过最大执行时间",
    ],
    CAT_PARAM_ERROR: [
        "date must not be the 30th of February",
        "非法的日期参数：2025-02-30",
    ],
}


def _msg(rng: random.Random, cls: str, mode: str = "") -> str:
    if mode == "heldout":
        return rng.choice(_HELD_OUT[cls])
    if mode == "paraphrase":
        return rng.choice(_PARAPHRASE[cls])
    return _CANONICAL[cls]


def _param_error(rng: random.Random, mode: str = "") -> Trace:
    sym, task = _task_price(rng)
    t = Trace(task=task, parser="rule")
    t.step("parse_query", arguments={"question": task}, result={"symbol": sym, "intent": "price"})
    t.step("build_sql", result={"sql": "..."})
    # 参数写错：日期不合法。硬错误，loud——但这属于 agent 能力问题。
    t.step(
        "execute_sql",
        arguments={"date": "2025-02-30"},
        status=STATUS_ERROR,
        error=_msg(rng, CAT_PARAM_ERROR, mode),
    )
    t.finish(final_correct=False)
    return t


def _tool_missing(rng: random.Random, mode: str = "") -> Trace:
    sym, task = _task_price(rng)
    t = Trace(task=task, parser="llm")
    t.step("parse_query", arguments={"question": task}, result={"symbol": sym, "intent": "price"})
    # 调了个宿主没注册的工具。这是配置问题，不是 agent 笨。
    t.step(
        "stock_news_search",
        arguments={"symbol": sym},
        status=STATUS_ERROR,
        error=_msg(rng, CAT_TOOL_MISSING, mode),
    )
    t.finish(final_correct=False)
    return t


def _env_timeout(rng: random.Random, mode: str = "") -> Trace:
    sym, task = _task_price(rng)
    t = Trace(task=task, parser="rule")
    t.step("parse_query", arguments={"question": task}, result={"symbol": sym, "intent": "price"})
    t.step("build_sql", result={"sql": "..."})
    # 沙箱超时：重跑一次可能就成了，不该当负样本。
    t.step("execute_sql", status=STATUS_ERROR, error=_msg(rng, CAT_ENV_TIMEOUT, mode))
    t.finish(final_correct=False)
    return t


def _retry_loop(rng: random.Random) -> Trace:
    sym, task = _task_price(rng)
    t = Trace(task=task, parser="llm")
    t.step("parse_query", arguments={"question": task}, result={"symbol": sym, "intent": "price"})
    for _ in range(rng.randint(3, 5)):
        t.step("execute_sql", result={"value": None})
    t.finish(final_correct=False)
    return t


def _hallucinated(rng: random.Random) -> Trace:
    # 问句只给月份，模型自己补了个「日」——槽位完整，校验放行，
    # 表面完全正常。这是最隐蔽的一类。
    sym = rng.choice(_SYMBOLS)
    task = f"{_NAMES[sym]} 2025 年 {rng.randint(3, 11)} 月的收盘价是多少？"
    t = Trace(task=task, parser="llm")
    t.step(
        "parse_query",
        arguments={"question": task},
        # 编的日子用 26–28：问句里几乎不可能出现，保证"查不出处"这个信号
        # 真的注进去了（早先用 04，会和"4 月"撞上，3/20 条漏注）。
        result={
            "symbol": sym,
            "intent": "price",
            "as_of": f"2025-{rng.randint(1, 12):02d}-{rng.choice([26, 27, 28])}",
        },
    )
    t.step("execute_sql", result={"value": round(rng.uniform(10, 200), 2)})
    t.step("trust_check", result={"badge": "BASELINE", "usable": True})
    t.finish(final_correct=True)  # 数字出来了，看着一切正常
    return t


def _lucky_guess(rng: random.Random) -> Trace:
    sym, task = _task_price(rng)
    t = Trace(task=task, parser="llm->rule")
    t.step(
        "parse_query",
        arguments={"question": task},
        result={"symbol": sym, "intent": "price"},
        status=STATUS_DEGRADED,
        error="price 缺 as_of",
    )
    t.step("execute_sql", result={"value": round(rng.uniform(10, 200), 2)})
    t.step("trust_check", result={"badge": "VERIFIED", "usable": True})
    # 结果是对的，但路径上走了兜底——教模型学这个等于教它蒙。
    t.finish(final_correct=True)
    return t


_GENERATORS: dict[str, Callable[..., Trace]] = {
    CAT_OK: _clean,
    CAT_PARAM_ERROR: _param_error,
    CAT_TOOL_MISSING: _tool_missing,
    CAT_ENV_TIMEOUT: _env_timeout,
    CAT_RETRY_LOOP: _retry_loop,
    CAT_HALLUCINATED: _hallucinated,
    CAT_LUCKY_GUESS: _lucky_guess,
}


def generate(
    per_class: int = 20, seed: int = 20260918, mode: str = ""
) -> Iterator[Trace]:
    """mode: "" = canonical；"paraphrase" = dev 改写集；"heldout" = 从不调参的措辞"""
    """确定性生成：同 seed 同结果，别人跑出来一模一样。

    mode="paraphrase" 用 dev 改写措辞（可调参）；mode="heldout" 用
    从没参与调参的措辞（只能看，不能据此改规则）。
    """
    for cls in CLASSES:
        rng = random.Random(f"{seed}-{cls}")
        for i in range(per_class):
            t = _GENERATORS[cls](rng, mode) if cls in _PARAPHRASE else _GENERATORS[cls](rng)
            # 根因写进 golden——这是整件事的意义：有 ground truth 才能
            # 反过来测归因器准不准。
            t.golden = {"root_cause": cls, "seed": seed, "idx": i}
            yield t
