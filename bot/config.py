from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    bot_token: str
    gemini_api_key: str = ""
    groq_api_key: str = ""

    # Имена моделей меняются у провайдеров часто, поэтому только через .env.
    gemini_model: str = "gemini-3.8-flash"
    groq_llm_model: str = "llama-3.3-70b-versatile"
    groq_vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
    groq_stt_model: str = "whisper-large-v3-turbo"

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
