"""findata ↔ mm-curation-pipeline / curation-eval 桥接层。

目标：让 findata 能消费 curation-eval 的协议（指标、算子、注入器），
并明确边界——不是所有 mm-curation 的东西都该被 findata 借，
只借"接口形态对得上"的部分。

## 真实可借
- `cohen_kappa` —— 通用一致性指标，findata 的 LLM vs 规则归因对比
  本来就需要这种"修正随机一致"的指标，补上
- `pr_from_drops` 的语义 —— findata 的告警精确率与它等价
  （P/R over "你认为要 drop 的样本"），但 findata 写在评测内核里
  比借外部更直接，所以只注释引用

## 不借（接口错位，硬接是厚适配）
- `Contaminator` —— 样本级（sample.id + image/text），findata 故障
  是表级（symbol + date + column），硬接要做"假 Sample"包装层
- `Operator` / `BatchOperator` —— 单条样本输入，findata 探针是
  `ProbeContext` 全表/全标的输入，调用方签名都不同

## 找包策略
1. 优先看是否已 pip install `curation-eval`
2. 否则看环境变量 `FINDATA_CURATION_EVAL_PATH`
3. 否则默认桌面路径 `~/Desktop/mm-curation-pipeline/packages/curation-eval/src`
4. 三种方式都没有 → 安静返回 None，调用方降级为本地实现
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType

_DEFAULT_REL = Path("~/Desktop/mm-curation-pipeline/packages/curation-eval/src").expanduser()

_cached: ModuleType | None | bool = False  # 缓存 + 失败标记


def import_curation_eval() -> ModuleType | None:
    """按优先级寻找 curation_eval 包，失败时返回 None。"""
    global _cached
    if _cached is not False:
        return _cached if _cached else None  # type: ignore[return-value]

    # 1. 已 pip install
    try:
        import curation_eval  # noqa: F401

        _cached = sys.modules["curation_eval"]
        return _cached
    except ImportError:
        pass

    # 2. 环境变量
    env_path = os.environ.get("FINDATA_CURATION_EVAL_PATH")
    if env_path:
        m = _try_add(Path(env_path))
        if m is not None:
            _cached = m
            return _cached

    # 3. 桌面默认路径
    m = _try_add(_DEFAULT_REL)
    if m is not None:
        _cached = m
        return _cached

    _cached = None
    return None


def _try_add(path: Path) -> ModuleType | None:
    if not path.exists():
        return None
    sys.path.insert(0, str(path))
    try:
        import curation_eval  # noqa: F401

        return sys.modules["curation_eval"]
    except ImportError:
        # 撤回刚才的 sys.path 修改，避免污染后续 import
        try:
            sys.path.remove(str(path))
        except ValueError:
            pass
        return None
