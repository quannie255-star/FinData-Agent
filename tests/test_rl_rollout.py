"""RL 轨迹采集测试：episode 循环、状态标注、GRPO 组、聚合、异常兜底。"""

from __future__ import annotations

import json

from findata.agent.llm import MockLLMClient
from findata.dq.triage import BaselineTriage
from findata.rl.policy import TriageAgentPolicy
from findata.rl.rollout import aggregate, rollout_episode, rollout_group
from findata.rl.types import RLTask, StepStatus


def _attr_task(item) -> RLTask:
    return RLTask(
        task_id="t1",
        data_source="attribution",
        env_class="attribution",
        prompt=(),
        ground_truth={
            "finding_key": item.finding.key,
            "expected_cause": item.expected_cause.value,
            "expected_suppressed": item.expected_suppressed,
        },
    )


def _submit_policy(item, ctx) -> TriageAgentPolicy:
    dx = BaselineTriage().diagnose(item.finding, ctx)
    reply = json.dumps({"action": "submit_attribution", "arguments": {
        "root_cause": dx.root_cause.value, "severity": dx.severity.value,
        "confidence": dx.confidence, "explanation": dx.explanation or "规则结论"}},
        ensure_ascii=False)
    return TriageAgentPolicy(MockLLMClient(default=reply))


class TestRolloutEpisode:
    def test_submit_first_episode(self, labeled, eval_result):
        from findata.rl.env import AttributionEnv

        task = _attr_task(labeled)
        ep = rollout_episode(
            task, AttributionEnv(labeled, eval_result.ctx), _submit_policy(labeled, eval_result.ctx)
        )
        assert ep.error is None
        assert ep.trajectory.n_turns == 1
        assert ep.trajectory.steps[0].status is StepStatus.FINISH
        assert ep.reward > 0.5  # 规则结论在该语料上近乎全对

    def test_tool_call_then_submit_marks_statuses(self, labeled, eval_result):
        from findata.rl.env import AttributionEnv

        replies = [
            '{"action": "query_evidence", "arguments": {}}',
            '{"action": "peer_comparison", "arguments": {}}',
            '{"action": "submit_attribution", "arguments": {"root_cause": "benign_suspension", '
            '"severity": "OK", "confidence": 0.5, "explanation": "停牌"}}',
        ]
        task = _attr_task(labeled)
        policy = TriageAgentPolicy(MockLLMClient(replies))
        ep = rollout_episode(task, AttributionEnv(labeled, eval_result.ctx), policy)
        statuses = [s.status for s in ep.trajectory.steps]
        assert statuses == [StepStatus.TOOL_CALL, StepStatus.TOOL_CALL, StepStatus.FINISH]

    def test_chat_snapshot_grows(self, labeled, eval_result):
        from findata.rl.env import AttributionEnv

        replies = [
            '{"action": "query_evidence", "arguments": {}}',
            '{"action": "submit_attribution", "arguments": {"root_cause": "benign_suspension", '
            '"severity": "OK", "confidence": 0.5, "explanation": "停牌"}}',
        ]
        policy = TriageAgentPolicy(MockLLMClient(replies))
        ep = rollout_episode(_attr_task(labeled), AttributionEnv(labeled, eval_result.ctx), policy)
        sizes = [len(s.chat) for s in ep.trajectory.steps]
        assert sizes[1] > sizes[0]  # 第二步的上下文包含第一步的工具观测

    def test_env_exception_becomes_error_episode(self, labeled, eval_result):
        from findata.rl.env import AttributionEnv

        class BoomEnv(AttributionEnv):
            def step(self, action):
                raise RuntimeError("环境崩了")

        ep = rollout_episode(
            _attr_task(labeled),
            BoomEnv(labeled, eval_result.ctx),
            _submit_policy(labeled, eval_result.ctx),
        )
        assert ep.error is not None
        assert ep.reward == 0.0


class TestAskRollout:
    def test_single_step_with_runner(self):
        from findata.rl.env import AskEnv

        task = RLTask(
            task_id="ask-000",
            data_source="ask",
            env_class="ask",
            prompt=({"role": "user", "content": "茅台最新收盘价是多少？"},),
            ground_truth={"values": [121.0], "unit": "元", "want_run_id": True, "tol": 0.01},
        )

        def runner(q: str) -> dict:
            return {
                "answer": f"{q[:-1]} 121.0 元 [run_id=abcd1234] ✓",
                "run_ids": ["abcd1234"],
                "tool_rounds": 1,
                "usage": {},
                "verdict": None,
                "mode": "direct",
            }

        ep = rollout_episode(task, AskEnv(runner), None)
        assert ep.error is None
        assert ep.trajectory.steps[-1].status is StepStatus.FINISH
        assert ep.reward == 1.0
        # 终态答案写入了轨迹（assistant 消息）
        assert "121.0" in ep.info["answer"]


class TestRolloutGroup:
    def test_group_same_task_n_rollouts(self, labeled, eval_result):
        from findata.rl.env import AttributionEnv

        task = _attr_task(labeled)
        group = rollout_group(
            task,
            env_factory=lambda: AttributionEnv(labeled, eval_result.ctx),
            policy_factory=lambda: _submit_policy(labeled, eval_result.ctx),
            n=3,
        )
        assert len(group.episodes) == 3
        assert all(e.reward >= 0.0 for e in group.episodes)
        assert all(e.task is task for e in group.episodes)


class TestAggregate:
    def test_means_and_bool_components(self, labeled, eval_result):
        from findata.rl.env import AttributionEnv

        task = _attr_task(labeled)
        env = AttributionEnv(labeled, eval_result.ctx)
        policy = _submit_policy(labeled, eval_result.ctx)
        eps = [rollout_episode(task, env, policy) for _ in range(2)]
        agg = aggregate(eps)
        assert agg["n"] == 2
        assert agg["n_errors"] == 0
        g = agg["by_data_source"]["attribution"]
        assert g["n"] == 2
        assert g["breakdown_mean"]["submitted"] == 1.0  # bool 分项参与均值
        assert 0.0 <= agg["reward_mean"] <= 1.0

    def test_empty(self):
        assert aggregate([]) == {"n": 0}
