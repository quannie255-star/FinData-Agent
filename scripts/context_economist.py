"""命令行包装：`uv run python scripts/context_economist.py --trace <dir> ...`

入口实现在 `findata.economist.cli`（那里也能作为 `findata-context-economist`
命令安装）。这里只做参数透传，避免同一套 CLI 有两份实现。
"""

from __future__ import annotations

import sys

from findata.economist.cli import main

if __name__ == "__main__":
    sys.exit(main())
