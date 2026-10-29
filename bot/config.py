from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

# Актуальные модели по умолчанию. Если в .env закреплено имя, которое провайдер уже убрал
# (404 «model not found»), бот переходит на эти.
DEFAULT_GROQ_LLM = "openai/gpt-oss-120b"
DEFAULT_GROQ_VISION = "qwen/qwen3.8-27b"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    bot_token: str
    gemini_api_key: str = ""
    groq_api_key: str = ""

    # Имена моделей меняются у провайдеров часто, поэтому только через .env.
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_model: str = "gemini-3.6-flash"  # если основная перегружена (503)
    groq_llm_model: str = DEFAULT_GROQ_LLM
    groq_vision_model: str = DEFAULT_GROQ_VISION
    groq_stt_model: str = "whisper-large-v3-turbo"

    # INFO для отладки, WARNING для сервера (в журнал попадают только ошибки).
    log_level: str = "INFO"

    db_path: Path = ROOT / "var" / "gymbro.db"
    default_tz: str = "Europe/Moscow"

    # Пусто = бот открыт для всех. Иначе только перечисленные Telegram ID.
    allowed_user_ids: Annotated[list[int], NoDecode] = []

    # Суточные лимиты на пользователя, чтобы не сжечь бесплатные квоты.
    limit_llm_per_day: int = 40
    limit_stt_per_day: int = 40
    limit_vision_per_day: int = 10

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _split_ids(cls, v):
        if isinstance(v, str):
            return [int(x) for x in v.replace(" ", "").split(",") if x]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
