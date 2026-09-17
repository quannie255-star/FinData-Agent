"""归因经验记忆（M13）：LLM 归因结论的跨 run 结构化沉淀与召回。

要解决的问题：LLM 归因器每次都是从零判断。同一种数据形态上周刚归因过、
这周又出现，先例应该被召回——但**记忆会污染**：一条像模像样的错误经验，
会让系统理直气壮地抑制真告警（与 PITCH Q3「一个像的事实比没有事实更
危险」同一原则）。

四条防线，每条都有对应单测：

1. **两阶段写入**（参考 TradingAgents 的 pending→reflection 时序）：
   归因即落 pending（零成本、零风险）；同指纹再次出现且结论一致才
   confirm。pending 不参与召回——单次观察不是经验，是待验证假设。
2. **置信门槛**：confidence < 0.6 的记忆不进 prompt（宁缺毋滥）。
   低置信记忆即使存在也**不得改变抑制决策**，这是验收门禁用例。
3. **冲突消解**：同一形态先判合法后判故障（或反之）→ 对立记忆权重
   减半；weight < 0.5 不再召回。记忆自己承认"这个形态我吃过反悔药"，
   比假装一贯正确诚实。
4. **时间点过滤**：召回只看 created_asof <= 查询 asof 的条目——回放
   历史时不得引用"事后才确认"的根因，防时间泄漏。

工程细节沿用 dq/history.py 的既有约定：事务原子写、幂等 upsert、
轮转上限（confirmed 每指纹留 5 条，pending 全局留 50 条）。
依赖方向：本模块只认 (fingerprint, Diagnosis) 与字符串先例，不 import
agent 层——指纹的"判别证据标签"由调用方（LLMTriage）作为 extra 传入。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date

import duckdb

from findata.dq.models import Diagnosis, Finding
from findata.dq.triage import _parse_window

# 低置信记忆不进 prompt：低于它说明连归因器自己都不确定，没有当经验的资格
MIN_CONFIDENCE = 0.6
# 冲突消解：每次矛盾权重减半，低于它说明这条经验反复翻车，不再召回
MIN_WEIGHT = 0.5
_CONFLICT_HALVING = 0.5
# 召回预算（TradingAgents 的经验：不控预算的召回迟早撑爆上下文）
SAME_FINGERPRINT_LIMIT = 5
CROSS_LESSON_LIMIT = 3
# 轮转上限：confirmed 每指纹保留条数 / pending 全局保留条数
MAX_CONFIRMED_PER_FINGERPRINT = 5
MAX_PENDING_TOTAL = 50

_COLS = (
    "fingerprint",
    "root_cause",
    "label",
    "status",
    "confidence",
    "weight",
    "hits",
    "lesson",
    "created_asof",
    "confirmed_asof",
    "updated_asof",
)


def _label_of(root_cause: str) -> str:
    return "benign" if str(root_cause).startswith("benign") else "fault"


def value_bucket(value: float) -> str:
    """粗分桶：指纹要跨 symbol/日期泛化，连续值必须离散化。

    桶宽刻意取数量级（而不是等宽）：5.1 和 5.4 该撞在一起（形态相同），
    1.2 和 4.6 不该（形态不同）。
    """
    v = abs(float(value))
    for bound in (0.0, 1.0, 2.0, 5.0, 8.0, 20.0, 100.0):
        if v < bound:
            return f"<{bound:g}"
    return ">=100"


def fingerprint(finding: Finding, ctx_asof: date, extra: Iterable[str] = ()) -> str:
    """形态指纹：同一类数据形态跨 symbol/日期稳定，判别证据标签可注入。

    组成 = probe | metric | 值分桶 | 尾窗与否 | extra 标签（如量额一致性分桶）。
    extra 由调用方从证据里提（agent 层计算，dq 层不 import agent）——
    这正是混淆对的解法：drift 故障与合法放量在 probe|metric|值分桶上
    同指纹，但 va_shift 分桶不同，不算同一形态。
    """
    parsed = _parse_window(finding.window)
    tail = "tail" if parsed is not None and parsed[1] >= ctx_asof else "inner"
    tags = "|".join(sorted(extra))
    return (
        f"{finding.probe}|{finding.metric}|{value_bucket(finding.value)}|{tail}"
        + (f"|{tags}" if tags else "")
    )


@dataclass
class RecallResult:
    """一次召回：同指纹全量先例 + 跨指纹一句话教训。空则不注入 prompt。"""

    same: list[dict] = field(default_factory=list)
    cross: list[dict] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.same or self.cross)

    def render(self) -> str:
        """渲染成 prompt 块。措辞必须带着「参考，不作判据」的框架——
        先例是线索，判断仍以本次证据为准。"""
        lines = ["历史先例（跨 run 已确认经验，仅供参考；最终判断必须以本次证据为准）："]
        for r in self.same:
            lines.append(
                f"- 同形态 [{r['fingerprint']}] 过去 {r['hits']} 次判为 "
                f"{r['root_cause']}（{r['label']}），置信 {r['confidence']:.0%}，"
                f"最近 {r['updated_asof']}"
                + (f"；教训：{r['lesson']}" if r.get("lesson") else "")
            )
        for r in self.cross:
            lines.append(f"- 相关形态 [{r['fingerprint']}]：{r['lesson']}")
        return "\n".join(lines)


class TriageMemory:
    """duckdb 持久化的归因经验记忆。写入两阶段，召回过四道防线。"""

    def __init__(self, conn: duckdb.DuckDBPyConnection) -> None:
        self.conn = conn
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS triage_memory (
                fingerprint    VARCHAR,
                root_cause     VARCHAR,
                label          VARCHAR,
                status         VARCHAR,
                confidence     DOUBLE,
                weight         DOUBLE,
                hits           BIGINT,
                lesson         TEXT,
                created_asof   DATE,
                confirmed_asof DATE,
                updated_asof   DATE,
                PRIMARY KEY (fingerprint, root_cause)
            )
            """
        )

    # ---- 写入（两阶段）----

    def observe(
        self,
        fp: str,
        diagnosis: Diagnosis,
        asof: date,
        lesson: str = "",
    ) -> str:
        """记录一次 LLM 归因观察，返回该条目状态：pending / confirmed。

        - 新指纹 → pending（单次观察只是假设）
        - pending 且同根因再次出现 → confirm（跨 run 复现，才配叫经验）
        - confirmed 且同根因再出现 → 刷 hits / lesson
        - 任何对立标签的已确认记忆存在 → 对立记忆权重减半（冲突消解）
        """
        cause = diagnosis.root_cause.value
        label = _label_of(cause)
        conf = max(0.0, min(1.0, float(diagnosis.confidence)))
        self._demote_conflicts(fp, label, asof)

        row = self._get(fp, cause)
        if row is None:
            self._insert(fp, cause, label, conf, asof, lesson)
            return "pending"
        if row["status"] == "pending":
            self._confirm(row, conf, asof, lesson)
            return "confirmed"
        self._touch(row, asof, lesson)
        return "confirmed"

    def _get(self, fp: str, cause: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM triage_memory WHERE fingerprint = ? AND root_cause = ?",
            [fp, cause],
        ).fetchone()
        return dict(zip(_COLS, row, strict=True)) if row else None

    def _demote_conflicts(self, fp: str, label: str, asof: date) -> None:
        opposite = "benign" if label == "fault" else "fault"
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "UPDATE triage_memory SET weight = weight * ?, updated_asof = ? "
                "WHERE fingerprint = ? AND label = ? AND status = 'confirmed'",
                [_CONFLICT_HALVING, asof, fp, opposite],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _insert(
        self, fp: str, cause: str, label: str, conf: float, asof: date, lesson: str
    ) -> None:
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO triage_memory VALUES "
                "(?, ?, ?, 'pending', ?, 1.0, 1, ?, ?, NULL, ?)",
                [fp, cause, label, conf, lesson, asof, asof],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _confirm(self, row: dict, conf: float, asof: date, lesson: str) -> None:
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "UPDATE triage_memory SET status = 'confirmed', confidence = ?, "
                "hits = hits + 1, lesson = ?, confirmed_asof = ?, updated_asof = ? "
                "WHERE fingerprint = ? AND root_cause = ?",
                [
                    max(row["confidence"], conf),
                    lesson or row["lesson"],
                    asof,
                    asof,
                    row["fingerprint"],
                    row["root_cause"],
                ],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _touch(self, row: dict, asof: date, lesson: str) -> None:
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                "UPDATE triage_memory SET hits = hits + 1, lesson = ?, updated_asof = ? "
                "WHERE fingerprint = ? AND root_cause = ?",
                [lesson or row["lesson"], asof, row["fingerprint"], row["root_cause"]],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    # ---- 召回（四道防线在这里生效）----

    def recall(self, fp: str, asof: date) -> RecallResult:
        """召回某指纹的先例。pending / 低置信 / 低权重 / 未来条目一律不出。"""
        same = self.conn.execute(
            "SELECT * FROM triage_memory WHERE fingerprint = ? AND status = 'confirmed' "
            'AND created_asof <= ? AND confidence >= ? AND weight >= ? '
            "ORDER BY hits DESC, updated_asof DESC LIMIT ?",
            [fp, asof, MIN_CONFIDENCE, MIN_WEIGHT, SAME_FINGERPRINT_LIMIT],
        ).fetchall()
        cross = self.conn.execute(
            "SELECT * FROM triage_memory WHERE fingerprint <> ? AND status = 'confirmed' "
            'AND created_asof <= ? AND confidence >= ? AND weight >= ? AND lesson <> \'\' '
            "ORDER BY updated_asof DESC, hits DESC LIMIT ?",
            [fp, asof, MIN_CONFIDENCE, MIN_WEIGHT, CROSS_LESSON_LIMIT],
        ).fetchall()
        to_dict = lambda rows: [dict(zip(_COLS, r, strict=True)) for r in rows]  # noqa: E731
        return RecallResult(same=to_dict(same), cross=to_dict(cross))

    # ---- 维护 ----

    def prune(self) -> int:
        """轮转：每指纹 confirmed 留最近 N 条，pending 全局留最近 M 条。
        返回删除行数。confirmed 是花了两次观察换来的，优先于 pending 保留。"""
        before = self.conn.execute("SELECT count(*) FROM triage_memory").fetchone()[0]
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                """
                DELETE FROM triage_memory WHERE status = 'confirmed' AND fingerprint IN (
                    SELECT fingerprint FROM (
                        SELECT fingerprint, root_cause,
                               row_number() OVER (
                                   PARTITION BY fingerprint ORDER BY updated_asof DESC
                               ) AS rn
                        FROM triage_memory WHERE status = 'confirmed'
                    ) WHERE rn > ?
                )
                """,
                [MAX_CONFIRMED_PER_FINGERPRINT],
            )
            self.conn.execute(
                """
                DELETE FROM triage_memory WHERE status = 'pending'
                AND (fingerprint, root_cause) IN (
                    SELECT fingerprint, root_cause FROM (
                        SELECT fingerprint, root_cause,
                               row_number() OVER (ORDER BY updated_asof DESC) AS rn
                        FROM triage_memory WHERE status = 'pending'
                    ) WHERE rn > ?
                )
                """,
                [MAX_PENDING_TOTAL],
            )
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        after = self.conn.execute("SELECT count(*) FROM triage_memory").fetchone()[0]
        return int(before - after)

    def all_entries(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM triage_memory").fetchall()
        return [dict(zip(_COLS, r, strict=True)) for r in rows]
