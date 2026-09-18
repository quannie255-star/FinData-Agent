"""自然语言 → 结构化查询参数。

**这一层最重要的约束：LLM 只做语言理解（填槽），不写 SQL、也不判可信。**

· SQL 由模板确定性生成——防注入、可审计、每次一样；
· 可信判定由 guard 的规则做——不交给概率模型。

这比「让 LLM 直接写 SQL」的 Text2SQL 路线窄得多，但窄得有理由：本项目卖的
是「这个数字能不能引用」，而这个判断必须可复现、可审计。把它交给一个每次
可能给出不同答案的模型，等于用不可信的手段去证明可信。

同理，LLM 的输出**一律不信任**：
· symbol 必须认得出来（在白名单里），认不出就降级规则解析；
· intent 必须是指定的三个之一；
· 日期必须能解析成 YYYY-MM-DD，缺年份不猜（猜错年份会让门禁在错误的
  区间上判可信，那比解析失败危险得多）；
· **槽位必须完整**：price 缺 as_of、区间缺 start/end 同样降级。半个槽
  比没有更危险——引擎会拿 None 去拼 SQL，门禁还会照着错误区间判可信。

任何一步校验失败都降级到确定性规则解析，绝不静默采用可疑结果。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from findata.agent.llm import LLMError

INTENTS = ("price", "change", "aggregate")

_PRICE_WORDS = ("收盘价", "价格", "股价", "多少钱")
_CHANGE_WORDS = ("涨", "跌", "涨幅", "跌幅", "涨了多少", "跌了多少")
# 「有成交」「多少个交易日」也算聚合：它们同样踩分母口径的坑。
_AGG_WORDS = ("日均", "平均", "多少天", "成交量", "成交额", "有成交", "多少个交易日")

SYSTEM_PROMPT = """你是查询参数提取器。把用户的自然语言问题转成 JSON，只输出 JSON，不要任何解释。

字段：
- symbol: 6 位股票代码，**必须**从候选列表里选；认不出来填 null
- intent: price（查某一天的价格）/ change（区间涨跌幅）/ aggregate（日均、总量、计数）
- as_of: 查单日价格时的日期，YYYY-MM-DD
- start / end: 区间查询的起止日期，YYYY-MM-DD

规则：
- 单日查询填 as_of；区间查询填 start 与 end；两者不要混用，不用的填 null
- 缺年份且无法从上下文推断时填 null，**不要猜年份**
- 不确定的字段填 null，不要编造

输出示例：{"symbol":"688981","intent":"price","as_of":"2025-09-04","start":null,"end":null}"""


@dataclass(frozen=True)
class ParsedQuery:
    symbol: str | None
    intent: str
    start: date | None
    end: date | None
    as_of: date | None


@dataclass(frozen=True)
class ParseOutcome:
    parsed: ParsedQuery
    source: str  # rule / llm / llm->rule（降级）
    usage_chars: int = 0
    error: str = ""
    # 模型原始输出。留着给归因用：只说"模型填错了"是猜测，
    # 把原文摆出来才是证据——本项目不接受没有证据的归因。
    raw: str = ""


def parse_rule(con, question: str) -> ParsedQuery:
    """确定性规则解析：零依赖、可离线、可单测。LLM 不可用时的兜底。"""
    symbol = None
    m = re.search(r"\b(\d{6})\b", question)
    if m:
        symbol = m.group(1)
    else:
        try:
            rows = con.execute("SELECT symbol, name FROM stock_universe").fetchall()
        except Exception:
            rows = []
        for sym, name in rows:
            if name and str(name) in question:
                symbol = str(sym)
                break

    intent = "price"
    if any(w in question for w in _AGG_WORDS):
        intent = "aggregate"
    elif any(w in question for w in _CHANGE_WORDS):
        intent = "change"

    # 中文日期：2025 年 9 月 4 日 / 9 月 4 日 / 2025-09-04（数字与单位间允许空格）
    ds: list[date] = []
    for y, mo, d in re.findall(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", question):
        ds.append(date(int(y), int(mo), int(d)))
    for mo, d in re.findall(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日", question):
        if not ds:
            # 没有年份可参照就不猜。猜错年份会让门禁在错误的区间上判可信，
            # 那比解析失败危险得多——沿用本项目「宁可 UNKNOWN 也不编」的口径。
            continue
        cand = date(ds[0].year, int(mo), int(d))
        if cand not in ds:
            ds.append(cand)
    for y, mo, d in re.findall(r"(\d{4})-(\d{1,2})-(\d{1,2})", question):
        cand = date(int(y), int(mo), int(d))
        if cand not in ds:
            ds.append(cand)

    # 只有年月：展开成整月区间
    if not ds:
        ym = re.findall(r"(\d{4})\s*年\s*(\d{1,2})\s*月", question)
        if ym:
            y, mo = int(ym[0][0]), int(ym[0][1])
            last = (date(y + (mo == 12), (mo % 12) + 1, 1)).toordinal() - 1
            return ParsedQuery(symbol, intent, date(y, mo, 1), date.fromordinal(last), None)

    ds.sort()
    if len(ds) >= 2:
        return ParsedQuery(symbol, intent, ds[0], ds[-1], None)
    if len(ds) == 1:
        return ParsedQuery(symbol, intent, None, None, ds[0])
    return ParsedQuery(symbol, intent, None, None, None)


def _universe_hint(con) -> str:
    try:
        rows = con.execute("SELECT symbol, name FROM stock_universe ORDER BY symbol").fetchall()
    except Exception:
        return ""
    return "、".join(f"{s}={n}" for s, n in rows if n)


def _extract_json(raw: str) -> dict | None:
    """从模型输出里抠出 JSON。模型爱在 JSON 外面套 markdown 代码块或废话。"""
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def _as_date(v) -> date | None:
    """解析日期。容忍 2025-08-1 这种个位写法，但非法日期照旧拒绝。

    为什么容忍：模型（尤其小模型）输出个位日期很常见，而 "2025-08-1"
    的含义**没有歧义**——接受它不是猜测。此前严格走 fromisoformat，
    把这类正确输出判成缺槽并降级，归因会给出"换更大模型"这种错误建议。
    容忍格式 ≠ 容忍有歧义的输入，日期越界（2 月 30 日）仍然拒绝。
    """
    if not v or not isinstance(v, str):
        return None
    m = re.fullmatch(r"\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*", v)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:  # 2 月 30 日这类：不是格式问题，是值不存在
        return None


def parse_with_llm(con, client, question: str) -> ParseOutcome:
    """LLM 填槽 + 白名单校验；任何一步不通过都降级到规则解析。"""
    hint = _universe_hint(con)
    user = f"候选股票：{hint}\n\n问题：{question}"
    try:
        raw = client.complete(SYSTEM_PROMPT, user)
    except (LLMError, Exception) as exc:  # 无 key / 连不上 Ollama / 超时
        return ParseOutcome(parse_rule(con, question), "llm->rule", 0, str(exc))

    usage = len(SYSTEM_PROMPT) + len(user) + len(raw)
    data = _extract_json(raw)
    if data is None:
        return ParseOutcome(parse_rule(con, question), "llm->rule", usage, "输出不是 JSON", raw)

    symbol = data.get("symbol")
    if symbol is None:
        # 模型认不出标的就别硬答。拿 null 去查库会在全表上跑，比认不出更危险。
        return ParseOutcome(parse_rule(con, question), "llm->rule", usage, "symbol 缺失", raw)
    symbol = str(symbol).strip()
    if not re.fullmatch(r"\d{6}", symbol):
        return ParseOutcome(parse_rule(con, question), "llm->rule", usage, "symbol 非 6 位", raw)
    try:
        known = {
            str(s) for (s,) in con.execute("SELECT DISTINCT symbol FROM stock_universe").fetchall()
        }
    except Exception:
        known = set()
    if known and symbol not in known:
        # 模型编了一个池子里没有的代码——不采用，降级。
        return ParseOutcome(parse_rule(con, question), "llm->rule", usage, "symbol 不在股票池", raw)

    intent = data.get("intent")
    if intent not in INTENTS:
        return ParseOutcome(
            parse_rule(con, question), "llm->rule", usage, f"intent 非法: {intent}", raw
        )

    parsed = ParsedQuery(
        symbol=symbol,
        intent=intent,
        start=_as_date(data.get("start")),
        end=_as_date(data.get("end")),
        as_of=_as_date(data.get("as_of")),
    )

    # 完整性校验：单日查询必须有 as_of，区间查询必须有起止两端。
    # 缺一半的槽比没有更危险——引擎会拿 None 去拼 SQL，等于在错误的
    # 区间上取数，而门禁会照着那个错误区间判「可信」。
    if intent == "price" and parsed.as_of is None:
        return ParseOutcome(parse_rule(con, question), "llm->rule", usage, "price 缺 as_of", raw)
    if intent != "price" and (parsed.start is None or parsed.end is None):
        return ParseOutcome(parse_rule(con, question), "llm->rule", usage, "区间缺 start/end", raw)

    return ParseOutcome(parsed, "llm", usage, "", raw)
