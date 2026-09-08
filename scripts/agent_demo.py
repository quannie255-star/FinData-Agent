"""Agent 对话入口：自然语言问数 → 带溯源的可信答案。

用法：
    uv run python scripts/agent_demo.py "贵州茅台最新收盘价是多少"
    uv run python scripts/agent_demo.py "600519 今年涨了多少"
    uv run python scripts/agent_demo.py "茅台现在的市盈率"
"""

from __future__ import annotations

import argparse

from findata.agent import answer_question
from findata.config import settings
from findata.core.db import connect


def main() -> int:
    parser = argparse.ArgumentParser(description="Findata 可信数据 Agent")
    parser.add_argument("question", help="自然语言问题")
    args = parser.parse_args()

    conn = connect(settings.dsn)
    try:
        answer = answer_question(conn, args.question)
        print(answer)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
