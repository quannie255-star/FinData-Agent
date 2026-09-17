"""告警推送：把巡检告警送到人手里的最后一公里。

支持四种国内常用的免费渠道（配置 FINDATA_NOTIFY_CHANNEL + WEBHOOK_URL）：

- ``wecom``      企业微信群机器人 webhook
- ``dingtalk``   钉钉群机器人 webhook（需在安全设置里加关键词，如"巡检"）
- ``feishu``     飞书群机器人 webhook
- ``serverchan`` Server酱（推到个人微信，URL 形如 https://sctapi.ftqq.com/<SendKey>.send）

设计红线只有一条：**推送失败绝不能打断巡检管线**。告警通道挂了是通道的
问题，巡检照常落库、报告照常归档——监控系统的自身故障不允许掩盖被监控
对象的故障。所以本模块对外只返回结果描述列表，不抛异常。
"""

from __future__ import annotations

import logging

import requests

from findata.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10.0

# 各渠道的请求体模板。统一发纯文本（markdown 在各家的渲染差异不值得兼容）。
_PAYLOADS: dict[str, object] = {
    "wecom": {"msgtype": "text", "text": {"content": "{title}\n{content}"}},
    "dingtalk": {"msgtype": "text", "text": {"content": "{title}\n{content}"}},
    "feishu": {"msg_type": "text", "content": {"text": "{title}\n{content}"}},
}


def render_alert_message(result) -> str:
    """InspectionResult → 推送文本。无告警时返回空串（调用方跳过推送）。

    推送阈值刻意收窄：P0 逐条推，P1 只在存在时推汇总，P2/抑制项不推——
    推送的价值在于"值得打断你"，日常 healthy 的日子保持安静，
    告警疲劳在推送渠道上的杀伤力比在面板上大得多。
    """
    s = result.summary
    p0 = result.p0
    p1 = [a for a in result.alerts if a.diagnosis.severity.value == "P1"]

    if not p0 and not p1:
        return ""

    lines = [
        f"健康分 {s.health_score:.0f}（{s.grade()}）· 告警 {s.n_alerts} · 抑制 {s.n_suppressed}"
    ]
    for a in p0:
        f, d = a.finding, a.diagnosis
        lines.append(f"[P0] {f.symbol} {d.root_cause.value}：{d.explanation}")
    if p1:
        lines.append(f"[P1] {len(p1)} 条：" + "；".join(f"{a.finding.symbol}" for a in p1[:5]))
        if len(p1) > 5:
            lines.append(f"（其余 {len(p1) - 5} 条见报告）")
    return "\n".join(lines)


def notify_text(title: str, content: str) -> list[str]:
    """推送到配置的渠道。返回结果描述（含失败原因），不抛异常。

    未配置渠道时返回 [说明]——每日管线在无配置环境下照常可跑。
    """
    channel = settings.notify_channel.strip().lower()
    url = settings.notify_webhook_url.strip()
    if not channel or not url:
        return ["推送未配置（FINDATA_NOTIFY_CHANNEL / FINDATA_NOTIFY_WEBHOOK_URL），已写日志"]

    results: list[str] = []
    try:
        payload = _build_payload(channel, title, content)
    except ValueError as e:
        return [f"未知推送渠道 {channel!r}：{e}"]

    # Server酱走表单字段，其余机器人走 JSON
    post_kwargs = {"data": payload} if channel == "serverchan" else {"json": payload}
    try:
        resp = requests.post(url, timeout=_TIMEOUT_SECONDS, **post_kwargs)
        ok = resp.status_code < 400
        results.append(f"{channel}: {'OK' if ok else f'HTTP {resp.status_code}'}")
    except requests.RequestException as e:
        results.append(f"{channel}: 推送失败（{e}）——巡检结果不受影响")
    for r in results:
        logger.info("告警推送 %s", r)
    return results


def _build_payload(channel: str, title: str, content: str) -> dict:
    if channel == "serverchan":
        # Server酱不接受 JSON body 的通用格式，走 data 字段（requests 自动表单化）
        return {"title": title[:32], "desp": f"**{title}**\n\n{content}"}  # type: ignore[return-value]
    template = _PAYLOADS.get(channel)
    if template is None:
        raise ValueError(f"支持 wecom/dingtalk/feishu/serverchan，收到 {channel!r}")
    return _fill(template, title=title, content=content)  # type: ignore[arg-type]


def _fill(obj, **kwargs):
    """递归填充 payload 模板里的 {title}/{content} 占位。"""
    if isinstance(obj, dict):
        return {k: _fill(v, **kwargs) for k, v in obj.items()}
    if isinstance(obj, str):
        return obj.format(**kwargs)
    return obj
