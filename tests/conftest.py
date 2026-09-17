"""共享 fixture：一份确定性评测结果，供 RL 环境测试复用。

run_evaluation 每次重建合成语料（约 1s），scope="module" 只建一次；
同参数必得同结果（项目铁律），所以模块级缓存不引入顺序耦合。
"""

from __future__ import annotations

import pytest

from findata.eval.evolution import build_labeled_items
from findata.eval.runner import run_evaluation


@pytest.fixture(scope="module")
def eval_result():
    return run_evaluation(fault_seed=20240102)


@pytest.fixture(scope="module")
def labeled(eval_result):
    """带标注语料的第一个条目（finding + expected_cause/suppressed）。"""
    items = build_labeled_items(eval_result)
    assert items, "语料对齐后必须产出标注条目"
    return items[0]


@pytest.fixture(scope="module")
def submit_action_factory():
    """把一个 Diagnosis 转成 submit_attribution 动作（测试辅助）。"""

    def _make(dx) -> dict:
        return {
            "action": "submit_attribution",
            "arguments": {
                "root_cause": dx.root_cause.value,
                "severity": dx.severity.value,
                "confidence": dx.confidence,
                "explanation": dx.explanation or "测试提交",
            },
        }

    return _make
