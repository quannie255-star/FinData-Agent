"""通用适配器包：把外部真实语料归一化成本项目的 Trace 形状。

只做形状转换与**确定性**校验，不做任何判断——判断在 `triage.py`。
这条边界和 `dq/probes.py` 一致：适配器负责把事实摆出来（工具注册表里
有没有这个名字、schema 有没有标 default），归因层负责定性。
"""

from __future__ import annotations
