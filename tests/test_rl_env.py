"""RL 环境测试：可复现性、工具确定性、submit 校验、超时终止、注册表。"""

from __future__ import annotations

import json

import pytest

from findata.dq.triage import BaselineTriage
from findata.rl.env import AskEnv, AttributionEnv, make, register


def test_reset_is_deterministic_same_seed(labeled, eval_result):
    """同 item + ctx，reset 观测逐字节一致——「环境可重置」的契约。"""
    a = AttributionEnv(labeled, eval_result.ctx).reset()
    b = AttributionEnv(labeled, eval_result.ctx).reset()
    assert a == b
    assert a[0]["role"] == "user"
    # 任务描述必须含信号概要与全部可用动作
    assert labeled.finding.symbol in a[0]["content"]
    for tool in ("query_evidence", "recall_memory", "peer_comparison", "submit_attribution"):
        assert tool in a[0]["content"]


def test_tool_results_are_deterministic(labeled, eval_result):
    """同一环境实例内重复查询，结果一致（证据是数据的函数，不是时间的）。"""
    env = AttributionEnv(labeled, eval_result.ctx)
    env.reset()
    r1 = env.step({"action": "query_evidence", "arguments": {}})
    r2 = env.step({"action": "query_evidence", "arguments": {}})
    assert r1.observations[0]["content"] == r2.observations[0]["content"]
    assert not r1.done and not r2.done
    assert r1.reward is None  # 中间步不计分（outcome 主干）


def test_query_evidence_returns_structured_facts(labeled, eval_result):
    env = AttributionEnv(labeled, eval_result.ctx)
    env.reset()
    raw = env.step({"action": "query_evidence", "arguments": {}}).observations[0]["content"]
    ev = json.loads(raw)
    assert ev["symbol"] == labeled.finding.symbol
    assert "has_ex_dividend" in ev


def test_peer_comparison_lists_surge_ratios(labeled, eval_result):
    env = AttributionEnv(labeled, eval_result.ctx)
    env.reset()
    raw = env.step({"action": "peer_comparison", "arguments": {}}).observations[0]["content"]
    doc = json.loads(raw)
    rows = next(iter(doc.values()))
    assert rows and any(r["is_target"] for r in rows)


def test_recall_memory_without_memory(labeled, eval_result):
    env = AttributionEnv(labeled, eval_result.ctx, memory=None)
    env.reset()
    r = env.step({"action": "recall_memory", "arguments": {}})
    assert "未启用记忆" in r.observations[0]["content"]


def test_submit_valid_ends_episode(labeled, eval_result, submit_action_factory):
    dx = BaselineTriage().diagnose(labeled.finding, eval_result.ctx)
    env = AttributionEnv(labeled, eval_result.ctx)
    env.reset()
    r = env.step(submit_action_factory(dx))
    assert r.done
    assert r.info["submitted"] is not None
    assert r.info["extra_turns"] == 0  # 正常提交不罚轮次
    with pytest.raises(RuntimeError):
        env.step(submit_action_factory(dx))  # 结束后 step 必须报错


def test_submit_rejects_illegal_cause(labeled, eval_result):
    env = AttributionEnv(labeled, eval_result.ctx)
    env.reset()
    r = env.step(
        {
            "action": "submit_attribution",
            "arguments": {
                "root_cause": "unknown",  # UNKNOWN 不允许作为结论
                "severity": "P0",
                "confidence": 0.9,
                "explanation": "x",
            },
        }
    )
    assert not r.done
    assert "错误" in r.observations[0]["content"]


def test_submit_rejects_unknown_action(labeled, eval_result):
    env = AttributionEnv(labeled, eval_result.ctx)
    env.reset()
    r = env.step({"action": "hack_the_db", "arguments": {}})
    assert not r.done
    assert "未知动作" in r.observations[0]["content"]


def test_max_turns_forces_timeout(labeled, eval_result):
    env = AttributionEnv(labeled, eval_result.ctx, max_turns=3)
    env.reset()
    state = None
    for _ in range(3):
        state = env.step({"action": "query_evidence", "arguments": {}})
    assert state.done
    assert state.info["timeout"]
    assert state.info["submitted"] is None
    assert state.info["extra_turns"] == 3  # 满额惩罚口径


def test_registry_make_and_unknown_id(labeled, eval_result):
    env = make("attribution", item=labeled, ctx=eval_result.ctx)
    assert isinstance(env, AttributionEnv)
    with pytest.raises(KeyError):
        make("no_such_env")


def test_register_routes_new_env():
    class Dummy:
        pass

    register("dummy_env_for_test", Dummy)
    assert isinstance(make("dummy_env_for_test"), Dummy)


def test_ask_env_without_runner_reports_error():
    env = AskEnv(runner=None)
    env.reset({"question": "茅台最新收盘价？"})
    r = env.step({})
    assert r.done
    assert r.info["error"]


def test_ask_env_single_step_collects_state():
    def runner(q: str) -> dict:
        return {
            "answer": f"答案：{q} 收盘价 121.0 元 run_id=abcd1234 ✓",
            "run_ids": ["abcd1234"],
            "tool_rounds": 2,
            "usage": {"total_tokens": 100},
            "verdict": "pass",
            "mode": "direct",
        }

    env = AskEnv(runner)
    msgs = env.reset({"question": "茅台最新收盘价是多少？"})
    assert msgs == [{"role": "user", "content": "茅台最新收盘价是多少？"}]
    r = env.step({})
    assert r.done
    assert r.info["answer"].startswith("答案：")
    assert r.info["run_ids"] == ["abcd1234"]
    assert r.info["error"] is None


def test_ask_env_runner_exception_is_contained():
    def bad_runner(q: str) -> dict:
        raise RuntimeError("boom")

    env = AskEnv(bad_runner)
    env.reset({"question": "q"})
    r = env.step({})
    assert r.done
    assert "boom" in r.info["error"]
