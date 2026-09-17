r"""每日数据管线：采集 → 事件采集 → 巡检 → 告警推送 → 报告归档（一条命令，供定时器调用）。

这是项目从「评测驱动的演示」走向「运维驱动的系统」的那一步：
调度器每天收盘后调它，它把数据采进仓库、巡检、把值得打断你的告警
推到手机，报告落盘归档——全程无人工。

事件采集（公司事件 + 停牌历史种子）自 R1.0 起常态化默认开启：
抑制规则靠 corporate_event 吃饭，表空 = 停牌/除权误报必然漏抑制
（见 docs/reviews/2026-09-15-attribution.md 根因一）。它跑在独立
子进程里，失败只记日志、**不阻塞**行情巡检——行情巡检是主输入，
知识层降级会在日报的知识层状态行里显式可见。

用法：
    uv run python scripts/daily_pipeline.py                 # 标准日更（含事件采集）
    uv run python scripts/daily_pipeline.py --no-events     # 跳过事件采集（调试）
    uv run python scripts/daily_pipeline.py --skip-ingest   # 只巡检（调试/首次演示）

调度（配置一次，之后无人值守；<ROOT> 换成项目绝对路径）：
    # Windows 本地（管理员 PowerShell，18:30 = 收盘后）：
    schtasks /create /tn "FindataDaily" /sc daily /st 18:30 /tr ^
      "cmd /c cd /d <ROOT> && .venv\Scripts\uv.exe run python ^
      scripts\daily_pipeline.py >> logs\daily\schtasks.log 2>&1"

    # Linux 云服务器（crontab -e，工作日 18:30）：
    30 18 * * 1-5  cd <ROOT> && uv run python scripts/daily_pipeline.py >> logs/daily/cron.log 2>&1

推送配置（.env，任一为空则只写日志不推送）：
    FINDATA_NOTIFY_CHANNEL=wecom            # wecom / dingtalk / feishu / serverchan
    FINDATA_NOTIFY_WEBHOOK_URL=https://...

退出码：0 全过；1 采集或巡检失败（调度器的日志里能看到）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

LOG_DIR = ROOT / "logs" / "daily"
REPORT_DIR = ROOT / "reports" / "daily"


def _run_step(cmd: list[str], log_name: str) -> tuple[int, str]:
    """跑一个采集子进程，返回 (退出码, 日志尾部)。

    无控制台会话（schtasks/cron）下捕获句柄可能为 None，显式 PIPE + 兜底，
    管线不许因日志崩——capture_output 的隐式行为在 schtasks 会话实测出过 None。
    """
    proc = subprocess.run(  # noqa: UP022
        cmd,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail = ((proc.stdout or "")[-20_000:]) + "\n--- stderr ---\n" + (proc.stderr or "")[-20_000:]
    (LOG_DIR / f"{log_name}-{datetime.now():%Y%m%d}.log").write_text(tail, encoding="utf-8")
    return proc.returncode, tail


def main() -> int:
    p = argparse.ArgumentParser(description="Findata 每日数据管线（采集→巡检→推送→归档）")
    p.add_argument(
        "--no-events", action="store_true", help="跳过公司事件采集（事件是抑制规则的知识源，慎关）"
    )
    p.add_argument("--skip-ingest", action="store_true", help="跳过采集，只巡检现有仓库")
    args = p.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    log(f"===== 每日管线开始 {datetime.now().isoformat(timespec='seconds')} =====")

    # ── 1. 行情采集（子进程跑已验证的 CLI；失败则无数据可巡检，直接退出）──
    if not args.skip_ingest:
        cmd = [sys.executable, str(ROOT / "scripts" / "ingest_finance.py")]
        log(f"行情采集开始：{' '.join(cmd[1:])}")
        code, _ = _run_step(cmd, "ingest")
        if code != 0:
            log(f"行情采集失败（exit={code}），详见 logs/daily/。本次不巡检。")
            return 1
        log("行情采集完成")

        # ── 2. 事件采集（独立步骤，常态化默认开；失败不阻塞巡检）──
        # 抑制规则靠 corporate_event 吃饭，事件采集挂了知识层就降级，
        # 降级本身由日报的知识层状态行显式暴露（P0d），不需要在这里硬失败。
        if not args.no_events:
            cmd_ev = [sys.executable, str(ROOT / "scripts" / "ingest_finance.py"), "--events-only"]
            log("事件采集开始（含停牌历史种子）")
            code_ev, _ = _run_step(cmd_ev, "events")
            if code_ev != 0:
                log(f"事件采集失败（exit={code_ev}），详见 logs/daily/。知识层将降级，继续巡检。")
            else:
                log("事件采集完成")

    # ── 2. 巡检真实仓库 ──
    from findata.config import settings
    from findata.core.db import connect
    from findata.report import Snapshot, render_html, render_markdown, run_inspection
    from findata.report.notify import notify_text, render_alert_message

    db = Path(settings.db_path)
    if not db.exists():
        log(f"仓库不存在：{db}。先跑一次 uv run python scripts/ingest_finance.py --full")
        return 1
    with connect(str(db), read_only=True) as conn:
        snapshot = Snapshot.from_duckdb(conn)
    if snapshot.stock_daily.empty:
        log("仓库无数据，巡检跳过。")
        return 1
    result = run_inspection(snapshot)
    s = result.summary
    log(
        f"巡检完成：asof={s.asof} 标的={s.n_symbols} 信号={s.n_findings} "
        f"告警={s.n_alerts} 抑制={s.n_suppressed} 健康分={s.health_score:.0f}"
    )

    # ── 3. 报告归档（先落盘再推送：推送挂了报告也在）──
    # 报告 Agent（R1.3）：市场解读 + 图表 + 可信审校（无徽章不得引用）。
    # LLM 未配 key 或调用失败自动降级确定性模板；Agent 整体失败不阻塞日报。
    agent_md, agent_html = "", ""
    try:
        from findata.agent.llm import OpenAICompatClient
        from findata.agent.nodes.report import generate_report_section

        client = OpenAICompatClient()
        section = generate_report_section(
            result, snapshot, llm_client=client if client.available else None
        )
        agent_md, agent_html = section.md, section.html
        log(
            f"报告 Agent：llm={'是' if section.llm_used else '模板'}"
            f" 审校拦截={len(section.review_notes)} 耗时={section.elapsed_ms}ms"
            f" 字符入/出={section.usage.get('chars_in', 0)}/{section.usage.get('chars_out', 0)}"
        )
    except Exception as exc:
        log(f"报告 Agent 失败（不影响巡检报告）：{type(exc).__name__}: {exc}")

    stamped = f"\n> 由每日管线自动生成于 {datetime.now():%Y-%m-%d %H:%M}\n"
    md = render_markdown(result, header_extra=stamped, footer_extra=agent_md)
    report_md = REPORT_DIR / f"{s.asof}.md"
    report_md.write_text(md, encoding="utf-8")
    (REPORT_DIR / f"{s.asof}.html").write_text(
        render_html(result, extra_sections=agent_html), encoding="utf-8"
    )
    log(f"报告归档：{report_md}")

    # ── 4. 告警推送（无告警保持安静；推送失败不影响管线退出码）──
    message = render_alert_message(result)
    if message:
        outcomes = notify_text(f"Findata 巡检告警 {s.asof}", message)
        for o in outcomes:
            log(f"推送：{o}")
    else:
        log("无 P0/P1 告警，保持安静（healthy 的日子不打扰）")

    log(f"===== 管线结束，耗时 {time.perf_counter() - t0:.0f}s =====")
    return 0


def log(msg: str) -> None:
    line = f"{datetime.now().isoformat(timespec='seconds')} {msg}"
    print(line, file=sys.stderr)
    with (LOG_DIR / f"{datetime.now():%Y%m%d}.log").open("a", encoding="utf-8") as f:
        f.write(line + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
