"""RL 策略侧（M14）：归因多轮窄 agent + 问数 runner。

归因策略刻意**不上 LangGraph**——ROADMAP 的诚实边界里写明
「监控层不上 LangGraph/多智能体」。这里只需要一个几十行的轻量循环：
把环境观测渲染成文本 → 调 LLMClient → 解析受控 JSON 动作。
与 LLMTriage 同样的不信任三防线：受控 JSON、白名单校验、失败可观测。

消息历史走 OpenAI chat 格式（与轨迹契约一致），但 LLMClient 的接口是
纯文本 complete(system, user)，因此由本模块负责把消息列表渲染成单文本。
若日后换支持原生多轮的客户端（如 Agent Lightning 的 rollout 级代理端点），
只需替换渲染层，环境与奖励不动。

问数任务的策略就是 LangGraph 多智能体图本身（agent 零改动——这正是
Agent Lightning 的接入哲学），make_ask_runner 只做 state → 奖励输入的
字段适配。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from findata.agent.llm import LLMClient, LLMError
from findata.dq.models import DIAGNOSABLE_CAUSES

# 与 rollout/env 的 max_turns 对齐的缺省轮数上限
MAX_TURNS = 6

# 窄 agent 版 system prompt。刻意比 LLMTriage._SYSTEM 简短：
# 证据不再一次全量注入，而是由模型按需索取——这是「多轮」的全部意义。
_SYSTEM_AGENT = """你是 A 股数据质量归因专家。当前有一个待归因的数据质量信号。

工作方式：每一轮你输出一个 JSON 动作，环境会返回对应结果：
- {{"action": "query_evidence", "arguments": {{}}}} —— 查看完整结构化证据
- {{"action": "recall_memory", "arguments": {{}}}} —— 召回历史先例（仅供参考）
- {{"action": "peer_comparison", "arguments": {{}}}} —— 同窗口其他标的量能对照
- {{"action": "submit_attribution", "arguments": {{"root_cause": "...", "severity": "...", "confidence": 0.9, "explanation": "..."}}}} —— 提交结论并结束

铁律：
1. 只依据工具返回的证据判断，不臆测。
2. root_cause 必须原样取自：{causes}
3. severity 只能取 P0/P1/P2/OK；合法业务事件（benign_*）一律 OK。
4. UNKNOWN 不允许作为结论；证据不足时按最保守的合法解释处理。
5. 只输出一个 JSON 对象，不要解释性文字、不要代码块标记。
6. 至多 {max_turns} 轮，务必在此之前提交。

输出格式：
{{"action": "...", "arguments": {{...}}}}"""


class TriageAgentPolicy:
    """归因窄 agent 策略：观测渲染 → LLM → 受控 JSON 动作。

    act 是无状态的（对话历史由调用方传入），同一实例可服务多个 episode；
    解析失败不抛异常——返回无效动作让环境报错并继续，轮次由环境兜底。
    """

    def __init__(
        self,
        client: LLMClient,
        system: str | None = None,
        max_turns: int = MAX_TURNS,
    ) -> None:
        self.client = client
        self.system = (system or _SYSTEM_AGENT).format(
            causes=", ".join(DIAGNOSABLE_CAUSES), max_turns=max_turns
        )
        self.max_turns = max_turns
        self.errors: list[str] = []
        self.calls = 0

    def act(self, messages: list[dict]) -> dict:
        """给定观测历史，返回下一个动作 {"action": name, "arguments": {...}}。"""
        self.calls += 1
        try:
            raw = self.client.complete(self.system, render_messages(messages))
        except LLMError as e:
            self.errors.append(f"llm_error: {e}")
            return _INVALID
        action = parse_action(raw)
        if action is None:
            self.errors.append(f"unparsable: {raw[:120]}")
            return _INVALID
        return action


# 解析失败的统一动作：环境按未知动作处理，报错并把轮次走完。
_INVALID = {"action": "__invalid__", "arguments": {}}


def render_messages(messages: list[dict]) -> str:
    """OpenAI 消息列表 → 单段文本（LLMClient 纯文本接口的桥）。"""
    role_name = {"user": "任务", "assistant": "你", "tool": "环境"}
    blocks = []
    for m in messages:
        who = role_name.get(m.get("role", ""), m.get("role", "?"))
        blocks.append(f"[{who}]\n{m.get('content', '')}")
    return "\n\n".join(blocks)


def parse_action(raw: str) -> dict | None:
    """从模型回复提取动作 JSON；不合法返回 None（调用方记 errors 并降级）。

    容忍代码块包裹与前后杂文本（对齐 LLMTriage._parse 的宽容度）。
    """
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`").replace("json", "", 1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        doc = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("action"), str):
        return None
    args = doc.get("arguments")
    return {
        "action": doc["action"],
        "arguments": args if isinstance(args, dict) else {},
    }


def make_ask_runner(conn: Any, arch: str = "multi") -> Callable[[str], dict]:
    """构造任务 B 的策略 runner：question → 奖励计算所需的最小字段集。

    multi = Supervisor 多智能体图（默认架构）；single = 单 Agent 回环
    （对比基线）。字段适配对齐 scripts/eval_agent.py 的 _answer_*，
    但不 import 脚本层——那是入口编排，不是可复用逻辑。
    """
    if arch == "multi":
        from findata.agent.supervisor import invoke_state

        def _run(question: str) -> dict:
            state = invoke_state(conn, question)
            final = state["messages"][-1]
            return {
                "answer": getattr(final, "content", "") or "",
                "run_ids": state.get("run_ids", []),
                "tool_rounds": state.get("tool_rounds", 0),
                "usage": state.get("usage", {}),
                "verdict": state.get("verdict"),
                "mode": state.get("mode"),
            }

        return _run

    from findata.agent.graph import build_agent

    def _run_single(question: str) -> dict:
        state = build_agent(conn).invoke(question)
        final = state["messages"][-1]
        return {
            "answer": getattr(final, "content", "") or "",
            "run_ids": state.get("run_ids", []),
            "tool_rounds": state.get("tool_rounds", 0),
            "usage": state.get("usage", {}),
            "verdict": None,
            "mode": None,
        }

    return _run_single
