"""trace 级语料漂移核验：两臂到底读的是不是同一份磁盘内容。

**为什么需要第二个方法**（不是重复劳动）：

| 方法 | 在哪儿比 | 能发现什么 | 发现不了什么 |
| --- | --- | --- | --- |
| 账单指纹（`corpus.py`） | 站在**现在**的磁盘上算 | 两个快照不同 | 快照**相同**、但运行期间被改过 |
| 本脚本（trace 级） | 回看**当时**落盘的产物 | 运行期间被改过 | 两臂没做过的调用 |

两者是**互补**的：指纹回答"这两个根是不是同一份"，trace 回答"跑的时候它变没变"。
2026-09-20 那次事故（边跑边提交）只有本方法能发现——**因为指纹那时还不存在**；
而如果当时有指纹，指纹只会显示"两臂的根路径不同"，说不出漂了多少。

**原理**：`payload_chars` 是**确定性量**——同一份内容必然给出同一个字符数。
所以同一个 `(任务, 工具, 参数)` 组合在两臂得到不同字符数，只有一个解释：
**工具读到的磁盘内容变了**。不需要归档原文也能做这个判断。

**这方法的盲区必须写出来**（否则它会被当成"没发现漂移＝没有漂移"）：
只能核**两臂都做过的调用**。两臂各自独有的调用（模型行为差异）无法核对。
所以输出里分别报「共有组合数」和「各自独有数」，并且**不把独有数算成漂移**。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from findata.contextbudget.stats import call_payload_index, load_runs

DEFAULT_DIR = Path("examples/context-audit")

# 显示用：参数 JSON 截断到这个长度，够看出是哪个调用，又不至于刷屏。
_ARG_PREVIEW = 58
_TASK_PREVIEW = 26


def _short(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def _print_only(label: str, keys: list) -> None:
    """打印"只在一臂出现"的调用（最多 10 条）。"""
    for task, tool, arg_json in keys[:10]:
        task_col = _short(task, _TASK_PREVIEW)
        args_col = arg_json[:_ARG_PREVIEW]
        print(f"    {label} {task_col:<{_TASK_PREVIEW}}  {tool:<14} {args_col}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="两臂 trace 级语料漂移核验")
    parser.add_argument("--dir", default=str(DEFAULT_DIR))
    parser.add_argument("--arm-a", required=True)
    parser.add_argument("--arm-b", required=True)
    return parser.parse_args()


def cross_check(index_a: dict, index_b: dict) -> dict[str, list]:
    """纯函数：给定两个 `call_payload_index`，分出漂移与独有。

    分成函数是为了可测 —— 这个判据本身（"字符数不同＝内容不同"）必须有测试
    钉住，否则它哪天静默失效也没人知道。
    """
    shared = set(index_a) & set(index_b)
    return {
        "shared": sorted(shared),
        "drift": sorted((k, index_a[k], index_b[k]) for k in shared if index_a[k] != index_b[k]),
        "only_a": sorted(set(index_a) - set(index_b)),
        "only_b": sorted(set(index_b) - set(index_a)),
    }


def main() -> int:
    args = parse_args()
    base = Path(args.dir)
    runs_a = load_runs(base / f"runs-{args.arm_a}.jsonl")
    runs_b = load_runs(base / f"runs-{args.arm_b}.jsonl")
    index_a = call_payload_index(runs_a)
    index_b = call_payload_index(runs_b)
    result = cross_check(index_a, index_b)

    n_shared = len(result["shared"])
    n_total = len(index_a) + len(index_b)

    print("=" * 78)
    print("trace 级语料漂移核验（判据：同一调用的 payload_chars 必须相同）")
    print("=" * 78)
    print(f"臂 A = {args.arm_a}   臂 B = {args.arm_b}")
    print(f"  A 的不同调用组合 = {len(index_a)}")
    print(f"  B 的不同调用组合 = {len(index_b)}")
    print(f"  两臂共有         = {n_shared}")
    print(f"  覆盖率           = {n_shared} / {n_total} 个组合可核 "
          f"（{n_shared / n_total:.1%}）" if n_total else "  覆盖率           = 无可核组合")

    drift = result["drift"]
    if not drift:
        print("\n  ✅ 共有组合里 payload_chars 不一致 = 0")
    else:
        print(f"\n  ❌ 共有组合里 payload_chars 不一致 = {len(drift)} —— **语料在运行期间变过**")
        for (task, tool, arg_json), ca, cb in drift:
            print(
                f"    {_short(task, _TASK_PREVIEW):<{_TASK_PREVIEW}}  {tool:<14} "
                f"{_short(arg_json, _ARG_PREVIEW):<{_ARG_PREVIEW}} A={ca:<8} B={cb}"
            )
        total_drift = sum(abs(cb - ca) for _, ca, cb in drift)
        print(f"    漂移字符规模 = {total_drift:,} 字符（|A−B| 求和，符号相反时不互相抵消）")

    print(
        f"\n  仅 A 有 = {len(result['only_a'])}   仅 B 有 = {len(result['only_b'])}"
        f"（模型行为差异，**不计入漂移**）"
    )
    _print_only("仅A", result["only_a"])
    _print_only("仅B", result["only_b"])

    print("\n--- 诚实说明 ---")
    print("  · 只核两臂**都做过**的调用；各自独有的调用**无法核对**——这是方法的盲区，")
    print("    不是'无漂移'的证据。独有数多时，本核验的效力就低。")
    print("  · 判据用的是确定性量（字符数），不需要归档工具返回原文")
    print("  · 这个核验**不能**替代语料指纹：它证明的是'跑的时候没变'，")
    print("    指纹证明的是'两个根是同一份'。两者都要。")
    return 1 if drift else 0


if __name__ == "__main__":
    sys.exit(main())
