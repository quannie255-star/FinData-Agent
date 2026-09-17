"""RL 环境（M14）：把确定性故障注入语料包装成可重置的多轮 agent 环境。

接口参照 SkyRL 的 BaseTextEnv（reset/step/close + gymnasium 式注册表），
但刻意留在进程内——本项目环境是纯 Python 确定性数据，不需要 AgentGym-RL
那样的 HTTP server-client，Protocol 足够；分布式训练是训练后端（verl/AGL）
的事，不是环境的事。

两个环境对应 ROADMAP 的两个任务：

- ``AttributionEnv``（任务 A）：episode = 一个数据质量信号的归因。
  策略观察信号概要 → 可查证据 / 召回记忆 / 同窗口对照 → 提交归因结论。
  这就是把 LLMTriage 的「一次批式调用」展开成**多轮窄 agent**：
  环境不替模型推理，只供应它索取的证据。
- ``AskEnv``（任务 B）：episode = 一个 golden 问题。LangGraph 多智能体图
  本身就是策略（Agent Lightning 的卖点：agent 零改动），环境只负责
  注入问题、回收 answer/run_ids/usage 供奖励计算。

确定性承诺：同样的 item + ctx，环境给出的观测序列逐字节一致。
这是「同 seed 可复现」与 GRPO 组内对比公平性的地基。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from findata.dq.memory import TriageMemory, fingerprint
from findata.dq.models import DIAGNOSABLE_CAUSES, Diagnosis, RootCause, Severity
from findata.dq.probes import ProbeContext
from findata.eval.evolution import LabeledItem
from findata.rl.types import StepResult

if TYPE_CHECKING:
    from findata.eval.runner import EvalResult

# 正常提交不罚轮次——多查证据是多轮窄 agent 的本意，不是浪费。
# 惩罚只针对超时未提交（extra_turns 语义见 AttributionEnv.step）。
_TIMEOUT_EXTRA_TURNS = 10


class RLEnv(Protocol):
    """环境协议（SkyRL BaseTextEnv 的进程内精简版）。

    system prompt 不属于环境——它是策略的参数（RL 语境下 prompt 与权重
    同属 policy），环境只提供 user 侧的观测。
    """

    @property
    def tools(self) -> list[dict]: ...

    def reset(self, task_desc: dict) -> list[dict]:
        """重置环境，返回初始观测（user 消息）。"""
        ...

    def step(self, action: dict) -> StepResult:
        """执行策略动作，返回观测 / 奖励（终态外为 None）/ 是否结束。"""
        ...

    def close(self) -> None: ...


# ─────────────────────────── 工具 schema（OpenAI function 格式） ───────────────────────────

_TOOLS = [
    {
        "name": "query_evidence",
        "description": "查询该信号的完整结构化证据（停牌覆盖、量额一致性、同窗口共振、除权线索等）",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "recall_memory",
        "description": "按形态指纹召回历史归因先例（仅供参考；没有先例时返回空）",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "peer_comparison",
        "description": "同窗口其他标的的量能变化对照（区分市场级行情与单点事故）",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "submit_attribution",
        "description": "提交归因结论，结束本 episode",
        "parameters": {
            "type": "object",
            "properties": {
                "root_cause": {"type": "string"},
                "severity": {"type": "string"},
                "confidence": {"type": "number"},
                "explanation": {"type": "string"},
            },
            "required": ["root_cause", "severity", "confidence", "explanation"],
        },
    },
]


class AttributionEnv:
    """任务 A 环境：一个 Finding 的多轮归因（episode 粒度 = 一个信号）。

    可用动作即 _TOOLS 四个；只有 submit_attribution 终止 episode。
    """

    def __init__(
        self,
        item: LabeledItem,
        ctx: ProbeContext,
        memory: TriageMemory | None = None,
        max_turns: int = 6,
    ) -> None:
        self.item = item
        self.ctx = ctx
        self.memory = memory
        self.max_turns = max_turns
        self._turns = 0
        self._done = False
        self._submitted: Diagnosis | None = None
        self._evidence: dict | None = None

    @property
    def tools(self) -> list[dict]:
        return _TOOLS

    def reset(self, task_desc: dict | None = None) -> list[dict]:
        f = self.item.finding
        self._turns = 0
        self._done = False
        self._submitted = None
        self._evidence = None
        brief = {
            "probe": f.probe,
            "table": f.table,
            "symbol": f.symbol,
            "window": f.window,
            "metric": f.metric,
            "value": f.value,
            "threshold": f.threshold,
            "severity_hint": f.severity_hint.value,
            "probe_evidence": f.evidence,
        }
        intro = (
            "请归因以下数据质量信号。先按需调用工具收集证据，再提交结论。\n"
            f"信号：{json.dumps(brief, ensure_ascii=False)}\n\n"
            "可用动作（每次回复一个 JSON）：\n"
            + "\n".join(f'- {{"action": "{t["name"]}", "arguments": {{...}}}}' for t in _TOOLS)
            + "\n\n约束：\n"
            f"1. root_cause 必须原样取自：{', '.join(DIAGNOSABLE_CAUSES)}\n"
            "2. severity 只能取 P0/P1/P2/OK；合法业务事件（benign_*）一律 OK\n"
            "3. 最终必须调用 submit_attribution 给出结论"
        )
        return [{"role": "user", "content": intro}]

    def step(self, action: dict) -> StepResult:
        if self._done:
            raise RuntimeError("episode 已结束，请先 reset")
        self._turns += 1
        name = str(action.get("action", ""))
        args = action.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}

        handler = {
            "query_evidence": self._tool_evidence,
            "recall_memory": self._tool_memory,
            "peer_comparison": self._tool_peers,
            "submit_attribution": self._tool_submit,
        }.get(name)

        if handler is None:
            names = ", ".join(t["name"] for t in _TOOLS)
            observation = f"错误：未知动作 {name!r}。可用动作：{names}"
        else:
            observation = handler(args)

        timeout = self._turns >= self.max_turns and not self._done
        if timeout:
            # 超时强制终止：提交记 None（奖励按未提交计），惩罚满额。
            self._done = True
            observation += "\n（已达最大轮数，episode 强制结束）"
        info = self._info(timeout)
        message = {"role": "tool", "content": observation, "tool_call_id": f"t{self._turns}"}
        return StepResult(
            observations=[message],
            reward=None,
            done=self._done,
            info=info,
        )

    def _info(self, timeout: bool) -> dict:
        return {
            "submitted": self._submitted,
            "timeout": timeout,
            # extra_turns 语义：正常提交=0；超时=max_turns（对齐 reward 的惩罚口径）
            "extra_turns": self.max_turns if (timeout and self._submitted is None) else 0,
            "turns": self._turns,
        }

    def _tool_evidence(self, _args: dict) -> str:
        if self._evidence is None:
            from findata.agent.llm_triage import build_evidence

            ev = build_evidence(self.item.finding, self.ctx)
            ev["index"] = 0
            ev["evidence"] = self.item.finding.evidence
            ev["note"] = self.item.note
            self._evidence = ev
        return json.dumps(self._evidence, ensure_ascii=False, indent=1)

    def _tool_memory(self, _args: dict) -> str:
        if self.memory is None:
            return "（未启用记忆）"
        extras = self._memory_extras()
        fp = fingerprint(self.item.finding, self.ctx.asof, extras)
        r = self.memory.recall(fp, self.ctx.asof)
        lines = [
            f"- 同形态 {row['fingerprint']}：过去 {row['hits']} 次判为 {row['root_cause']}"
            + (f"；{row['lesson']}" if row.get("lesson") else "")
            for row in r.same
        ]
        lines += [f"- 相关形态：{row['lesson']}" for row in r.cross]
        if not lines:
            return "（无已确认先例）"
        return "历史先例（仅供参考，最终判断以本次证据为准）：\n" + "\n".join(lines)

    def _memory_extras(self) -> tuple[str, ...]:
        """量额一致性 / 同窗口共振的指纹标签（与 LLMTriage._memory_extras 同源语义）。"""
        if self._evidence is None:
            return ()
        extras: list[str] = []
        from findata.dq.memory import value_bucket

        if "volume_amount_ratio_shift" in self._evidence:
            extras.append(f"va_shift:{value_bucket(float(self._evidence['volume_amount_ratio_shift']))}")
        if "same_window_peers" in self._evidence:
            extras.append("peers" if self._evidence["same_window_peers"] else "solo")
        return tuple(extras)

    def _tool_peers(self, _args: dict) -> str:
        """同窗口各标的的量能倍率对照（市场级行情会多标的共振，事故是单点）。"""
        frame = self.ctx.stock_daily
        f = self.item.finding
        from findata.dq.triage import _parse_window

        parsed = _parse_window(f.window)
        if frame.empty or parsed is None or "volume" not in frame.columns:
            return "（无对照数据）"
        start, end = parsed
        rows = []
        for symbol, g in frame.groupby("symbol"):
            g = g.sort_values("date")
            win = g[(g["date"] >= start) & (g["date"] <= end)]["volume"].astype(float)
            prior = g[g["date"] < start].tail(30)["volume"].astype(float)
            if win.empty or prior.empty or float(prior.mean()) <= 0:
                continue
            rows.append(
                {
                    "symbol": str(symbol),
                    "surge_ratio": round(float(win.mean()) / float(prior.mean()), 2),
                    "is_target": symbol == f.symbol,
                }
            )
        rows.sort(key=lambda r: -r["surge_ratio"])
        shown = rows[:8]
        # 目标标的是对照的主角，倍率再低也必须在场（否则"我为什么没放量"无从对照）
        if not any(r["is_target"] for r in shown):
            target = next((r for r in rows if r["is_target"]), None)
            if target:
                shown = shown[:7] + [target]
        return json.dumps({"同窗口量能倍率（近 30 日均值 vs 窗口均值）": shown}, ensure_ascii=False)

    def _tool_submit(self, args: dict) -> str:
        """白名单校验对齐 LLMTriage：非法提交被拒收，episode 不结束，可重试。"""
        try:
            cause = RootCause(str(args.get("root_cause", "")).strip())
        except ValueError:
            return f"错误：root_cause 非法。必须原样取自：{', '.join(DIAGNOSABLE_CAUSES)}"
        if cause is RootCause.UNKNOWN:
            return "错误：UNKNOWN 不允许作为提交结论（说不知道没有信息量）"
        try:
            severity = Severity(str(args.get("severity", "")).strip())
        except ValueError:
            return "错误：severity 非法。只能取 P0/P1/P2/OK"
        explanation = str(args.get("explanation", "")).strip()
        if not explanation:
            return "错误：explanation 不能为空"
        try:
            confidence = float(args.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        self._submitted = Diagnosis(
            finding_key=self.item.finding.key,
            root_cause=cause,
            severity=severity,
            confidence=max(0.0, min(1.0, confidence)),
            explanation=explanation,
            evidence_refs=("rl_agent",),
        )
        self._done = True
        return "已提交。"

    def close(self) -> None:
        self._evidence = None


class AskEnv:
    """任务 B 环境：一个 golden 问题（episode = 单步，LangGraph 图即策略）。

    策略侧第一次 step 时整体执行 supervisor 图；环境不做任何推理，
    只负责注入问题与回收产物。answer 本身是 assistant 终态消息，
    由 rollout 层写入轨迹，环境不再回观测。
    """

    def __init__(
        self,
        runner: Any = None,
        max_turns: int = 1,
    ) -> None:
        # runner: callable[[str], dict]，语义同 supervisor.invoke_state
        # None = 未配置（step 如实报 error）
        self.runner = runner
        self.max_turns = max_turns
        self._question = ""
        self._turns = 0
        self._done = False

    @property
    def tools(self) -> list[dict]:
        return []  # 工具在图内部，对策略整体不可见

    def reset(self, task_desc: dict) -> list[dict]:
        self._question = str(task_desc.get("question", ""))
        self._turns = 0
        self._done = False
        return [{"role": "user", "content": self._question}]

    def step(self, _action: dict) -> StepResult:
        if self._done:
            raise RuntimeError("episode 已结束，请先 reset")
        self._turns += 1
        self._done = True
        if self.runner is None:
            # runner 未注入（如无 LLM key 的 mock 模式）：如实报 error 而非假装失败
            info = {
                "answer": "",
                "run_ids": [],
                "tool_rounds": 0,
                "usage": {},
                "verdict": None,
                "mode": None,
                "error": "runner 未配置（无 LLM key 或未连接仓库）",
            }
            return StepResult(observations=[], reward=None, done=True, info=info)
        try:
            state = dict(self.runner(self._question))
            answer = str(state.get("answer", ""))
            info = {
                "answer": answer,
                "run_ids": state.get("run_ids", []),
                "tool_rounds": state.get("tool_rounds", 0),
                "usage": state.get("usage", {}),
                "verdict": state.get("verdict"),
                "mode": state.get("mode"),
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001 — 单题失败不该拖垮整场 rollout
            info = {
                "answer": "",
                "run_ids": [],
                "tool_rounds": 0,
                "usage": {},
                "verdict": None,
                "mode": None,
                "error": str(exc),
            }
        return StepResult(observations=[], reward=None, done=True, info=info)

    def close(self) -> None:
        self._question = ""


# ─────────────────────────── 注册表（gymnasium / SkyRL 式） ───────────────────────────

_ENVS: dict[str, type] = {}


def register(name: str, env_cls: type) -> None:
    """注册环境类。id 即 RLTask.env_class，训练数据按行路由到环境。"""
    _ENVS[name] = env_cls


def make(name: str, **kwargs) -> Any:
    """按注册 id 构造环境实例（等价 gymnasium.make 的进程内版）。"""
    if name not in _ENVS:
        raise KeyError(f"未注册的环境：{name}。已注册：{sorted(_ENVS)}")
    return _ENVS[name](**kwargs)


register("attribution", AttributionEnv)
register("ask", AskEnv)


# ─────────────────────────── 任务构建 ───────────────────────────


def build_attribution_tasks(
    result: EvalResult,
    seed: int,
    max_turns: int = 6,
) -> list[tuple]:
    """一次评测结果 → [(任务定义, 环境实例)]。

    标注对齐复用 evolution.build_labeled_items（故障优先，与 M13 进化器
    同一份语料——RL 任务集与归因评测不许是两套口径）。prompt 在构建时
    由环境生成并冻结进任务定义，训练侧数据行因此自包含（verl 拿到行
    就能训，不需要环境在场）。
    """
    from findata.eval.evolution import build_labeled_items
    from findata.rl.types import ATTRIBUTION, RLTask

    out: list[tuple] = []
    for i, item in enumerate(build_labeled_items(result)):
        env = AttributionEnv(item, result.ctx, max_turns=max_turns)
        messages = env.reset()
        task = RLTask(
            task_id=f"attr-{seed}-{i:03d}",
            data_source=ATTRIBUTION,
            env_class="attribution",
            prompt=tuple(messages),
            ground_truth={
                "finding_key": item.finding.key,
                "expected_cause": item.expected_cause.value,
                "expected_suppressed": item.expected_suppressed,
            },
            extra_info={
                "seed": seed,
                "probe": item.finding.probe,
                "symbol": item.finding.symbol,
                "note": item.note,
            },
        )
        out.append((task, env))
    return out


def build_ask_tasks(cases: list[dict], runner: Any) -> list[tuple]:
    """golden 集 → [(任务定义, 环境)]。runner 即 supervisor.invoke_state 闭包。

    ground_truth 形状与 ask_reward 的 expect 参数一致（含 tol），
    保证 compute_reward 的 ask 分支零适配。
    """
    from findata.rl.types import ASK, RLTask

    out: list[tuple] = []
    for i, case in enumerate(cases):
        expect = dict(case.get("expect", {}))
        expect.setdefault("tol", float(case.get("tol", 1e-3)))
        env = AskEnv(runner)
        task = RLTask(
            task_id=f"ask-{i:03d}-{case.get('name', i)}",
            data_source=ASK,
            env_class="ask",
            prompt=( {"role": "user", "content": str(case["question"])}, ),
            ground_truth=expect,
            extra_info={"mode": case.get("mode"), "name": case.get("name", "")},
        )
        out.append((task, env))
    return out
