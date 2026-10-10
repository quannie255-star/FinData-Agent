"""全局配置：通过环境变量 / .env 覆盖，前缀 FINDATA_。

例如 .env 中：
    FINDATA_LLM_API_KEY=sk-xxx
    FINDATA_LLM_BASE_URL=https://api.deepseek.com
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_prefix="FINDATA_", extra="ignore"
    )

    # ---- 存储 ----
    data_dir: Path = PROJECT_ROOT / "data"
    db_path: Path = PROJECT_ROOT / "data" / "warehouse.duckdb"

    # ---- 金融数据采集 ----
    ingest_start_date: str = "20220101"
    ingest_sleep_seconds: float = 0.4  # akshare 数据源礼貌限速
    ingest_max_retries: int = 3

    # ---- LLM（M2+ 启用，OpenAI 兼容接口）----
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_timeout_seconds: float = 60.0

    # ---- AFAC 赛题四主线：阿里云百炼（DashScope，OpenAI 兼容接口）----
    # 推理问答只许 Qwen（docs/afac-track4.md §0.1），与上面通用 LLM 配置分开
    dashscope_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    dashscope_api_key: str = ""
    dashscope_model: str = "qwen-plus"
    dashscope_timeout_seconds: float = 120.0

    # ---- finqa 个人版混合检索（L2 深化，产品口径 2026-10-10 所有者裁决）----
    # 赛题口径评测线不受此开关影响（不 import 向量模块）。
    # hybrid_retrieval=False → 纯 BM25F；True → BM25F+向量 RRF（embedding
    # 需要 extra=embed，本机推理，文本不出本机；未装则显式回退 BM25F）。
    finqa_hybrid_retrieval: bool = False
    finqa_embed_backend: str = "fastembed"  # fastembed（本机）/ dashscope（云端兜底）
    finqa_embed_model: str = ""  # 空 = 按语料语言自动选 bge-small en/zh
    finqa_embed_cache_dir: Path = PROJECT_ROOT / "data" / "embed-cache"

    # ---- 告警推送（每日管线 scripts/daily_pipeline.py 用）----
    # channel 取 wecom（企业微信机器人）/ dingtalk / feishu / serverchan（Server酱）；
    # 两者任一为空 = 不推送，只写运行日志。
    notify_channel: str = ""
    notify_webhook_url: str = ""

    @property
    def dsn(self) -> str:
        return str(self.db_path)


settings = Settings()
