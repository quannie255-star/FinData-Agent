"""Qwen 客户端（阿里云百炼 OpenAI 兼容接口）+ token 台账。

两条不可破坏的约束（docs/afac-track4.md §0.1 / §0.3）：

1. **推理问答只许 Qwen**——本客户端是正式答题阶段唯一的模型出口；
2. **token 台账覆盖全部调用**（检索摘要、压缩、证据判断、答案生成、自检）——
   任何一次调用必须记账；接口未返回 usage 直接抛错，**不许静默估算**，
   估算出来的 token 进提交文件就是伪造账单。

付费 API 不进 CI（仓库铁律）：所有依赖本模块的测试必须注入假客户端。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from findata.config import settings


@dataclass(frozen=True)
class DecodingParams:
    """统一解码参数：对照实验的"只有一处不同"从固定这些开始。

    seed 与 temperature 固定是 v4.0 的教训——不固定解码，"同一配置再跑一次
    差多少"这个噪声下界永远测不出来（docs/r5.0-acceptance.md）。
    """

    temperature: float = 0.0
    top_p: float = 1.0
    seed: int = 20261005
    max_tokens: int = 1024


@dataclass
class CallRecord:
    qid: str
    purpose: str  # answer / retrieve / compress / verify …，台账按用途可分账
    model: str
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class TokenLedger:
    """全程 token 台账：题目内多轮、跨题目汇总都从这里出数。"""

    records: list[CallRecord] = field(default_factory=list)

    def add(self, record: CallRecord) -> None:
        self.records.append(record)

    def totals(self) -> tuple[int, int, int]:
        p = sum(r.prompt_tokens for r in self.records)
        c = sum(r.completion_tokens for r in self.records)
        return p, c, p + c

    def by_qid(self, qid: str) -> tuple[int, int, int]:
        rows = [r for r in self.records if r.qid == qid]
        p = sum(r.prompt_tokens for r in rows)
        c = sum(r.completion_tokens for r in rows)
        return p, c, p + c


class QwenClient:
    """正式答题阶段唯一的模型出口；key 缺失时给出可照做的报错。"""

    def __init__(
        self,
        model: str | None = None,
        params: DecodingParams | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model or settings.dashscope_model
        self.params = params or DecodingParams()
        self.api_key = api_key if api_key is not None else settings.dashscope_api_key
        self.base_url = base_url or settings.dashscope_base_url
        if not self.api_key:
            raise RuntimeError(
                "缺少 DashScope API key：在 .env 配置 FINDATA_DASHSCOPE_API_KEY"
                "（阿里云百炼控制台获取），或构造 QwenClient(api_key=...) 注入"
            )
        # 延迟导入：findata 其余部分不依赖 openai 包
        from openai import OpenAI

        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=settings.dashscope_timeout_seconds,
        )

    def chat(
        self, qid: str, purpose: str, messages: list[dict[str, str]]
    ) -> tuple[str, CallRecord]:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.params.temperature,
            top_p=self.params.top_p,
            seed=self.params.seed,
            max_tokens=self.params.max_tokens,
        )
        usage = resp.usage
        if usage is None:
            raise RuntimeError(
                f"[{qid}] 接口未返回 usage——token 台账不能缺记，禁止估算后继续"
            )
        record = CallRecord(
            qid=qid,
            purpose=purpose,
            model=self.model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )
        content = resp.choices[0].message.content or ""
        return content, record
