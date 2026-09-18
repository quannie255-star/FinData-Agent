"""ChatBI 薄骨架 + 可信门禁。

骨架（engine）负责取数，解析（parser）负责理解问题，
门禁（guard）负责判定「这个数字能不能引用」。

分层的原因见各自模块文档：取数与解析都是红海，判定是空白。
"""

from findata.chatbi.engine import Answer, answer, answer_structured
from findata.chatbi.guard import GuardResult, guard_query
from findata.chatbi.parser import ParsedQuery, ParseOutcome, parse_rule, parse_with_llm

__all__ = [
    "Answer",
    "answer",
    "answer_structured",
    "GuardResult",
    "guard_query",
    "ParseOutcome",
    "ParsedQuery",
    "parse_rule",
    "parse_with_llm",
]
