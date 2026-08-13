"""Runtime configuration, read from environment / `.env`."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 09 §8 allows storage to be downgraded to in-memory; the demo still runs.
    storage_backend: Literal["memory", "supabase"] = "memory"
    supabase_url: str = ""
    supabase_key: str = ""

    openai_api_key: str = ""

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
