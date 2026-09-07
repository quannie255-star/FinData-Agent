"""CI smoke 断言脚本：从 stdin 读 inspect 的 JSON 响应，校验顶层契约。

被 .github/workflows/test.yml 的 smoke step 调用：

    echo "$payload" | python3 scripts/ci_assert_inspect.py

为什么单独成文件而不是 inline 在 workflow 里：
- workflow 是 yaml literal block，内嵌多行 Python 会撞 yaml 缩进规则
  （line 88 起必须缩进到 run: | 块内，否则 yaml 解析器报错；line 99 收尾又必须不缩进）
- 把断言抽出来：yaml 里只剩一行 python 调用，干净且能在本地开发期单测

本地测试：
    curl -fsS -X POST -H 'content-type: application/json' \\
        -d '{"source":"synthetic","seed":20240102}' \\
        http://127.0.0.1:8080/v1/inspect \\
        | python3 scripts/ci_assert_inspect.py
"""

from __future__ import annotations

import json
import sys

REQUIRED_TOP_KEYS = ("asof", "summary", "alerts", "suppressed")
REQUIRED_SUMMARY_KEYS = (
    "n_findings",
    "n_alerts",
    "n_suppressed",
    "health_score",
    "grade",
)


def main() -> int:
    data = json.loads(sys.stdin.read())

    # 顶层契约：inspect 返回的 JSON 必有的 key
    for k in REQUIRED_TOP_KEYS:
        if k not in data:
            print(f"FAIL: missing top-level key '{k}': {list(data)}", file=sys.stderr)
            return 1

    summary = data["summary"]
    for k in REQUIRED_SUMMARY_KEYS:
        if k not in summary:
            print(
                f"FAIL: missing summary.{k}: {list(summary)}",
                file=sys.stderr,
            )
            return 1

    if not isinstance(data["alerts"], list):
        print(f"FAIL: alerts must be list, got {type(data['alerts'])}", file=sys.stderr)
        return 1

    print(
        f"inspect ok, n_findings={summary['n_findings']} "
        f"n_alerts={summary['n_alerts']} "
        f"n_suppressed={summary['n_suppressed']} "
        f"health={summary['health_score']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())