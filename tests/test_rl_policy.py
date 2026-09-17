"""RL 策略测试：受控 JSON 动作解析、不信任防线（非法输出降级）、消息渲染。"""

from __future__ import annotations

from findata.agent.llm import LLMError, MockLLMClient
from findata.rl.policy import TriageAgentPolicy, parse_action, render_messages


class TestParseAction:
    def test_plain_json(self):
        a = parse_action('{"action": "query_evidence", "arguments": {}}')
        assert a == {"action": "query_evidence", "arguments": {}}

    def test_code_fence_tolerated(self):
        a = parse_action('```json\n{"action": "recall_memory", "arguments": {}}\n```')
        assert a["action"] == "recall_memory"

    def test_surrounding_text_tolerated(self):
        a = parse_action('好的。{"action": "submit_attribution", "arguments": {}} 以上。')
        assert a["action"] == "submit_attribution"

    def test_garbage_returns_none(self):
        assert parse_action("我不知道该做什么") is None
        assert parse_action("") is None

    def test_non_dict_arguments_coerced(self):
        a = parse_action('{"action": "query_evidence", "arguments": "should be dict"}')
        assert a["arguments"] == {}


class TestRenderMessages:
    def test_roles_rendered_in_order(self):
        msgs = [
            {"role": "user", "content": "任务"},
            {"role": "tool", "content": "证据"},
            {"role": "assistant", "content": "动作"},
        ]
        text = render_messages(msgs)
        assert text.index("[任务]") < text.index("[环境]") < text.index("[你]")
        assert "证据" in text


class TestTriageAgentPolicy:
    def test_multi_turn_with_mock_queue(self):
        """脚本化两轮：先查证据，再提交——动作按队列依序返回。"""
        replies = [
            '{"action": "query_evidence", "arguments": {}}',
            '{"action": "submit_attribution", "arguments": {"root_cause": "upstream_stale", '
            '"severity": "P0", "confidence": 0.9, "explanation": "停更"}}',
        ]
        policy = TriageAgentPolicy(MockLLMClient(replies))
        a1 = policy.act([{"role": "user", "content": "任务"}])
        assert a1["action"] == "query_evidence"
        a2 = policy.act([{"role": "user", "content": "任务"}, {"role": "tool", "content": "证据"}])
        assert a2["action"] == "submit_attribution"
        assert policy.errors == []

    def test_unparsable_output_degrades_to_invalid_action(self):
        """不信任防线：模型输出垃圾 → 无效动作（环境报错并继续），不抛异常。"""
        policy = TriageAgentPolicy(MockLLMClient(default="完全不是 JSON"))
        a = policy.act([{"role": "user", "content": "x"}])
        assert a["action"] == "__invalid__"
        assert policy.errors and policy.errors[0].startswith("unparsable")

    def test_llm_error_degrades(self):
        class BoomClient:
            def complete(self, system: str, user: str) -> str:
                raise LLMError("超时")

        policy = TriageAgentPolicy(BoomClient())
        a = policy.act([{"role": "user", "content": "x"}])
        assert a["action"] == "__invalid__"
        assert policy.errors[0].startswith("llm_error")

    def test_system_prompt_contains_whitelist(self):
        policy = TriageAgentPolicy(MockLLMClient())
        assert "upstream_stale" in policy.system
        assert "submit_attribution" in policy.system
