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

    @property
    def dsn(self) -> str:
        return str(self.db_path)


settings = Settings()
