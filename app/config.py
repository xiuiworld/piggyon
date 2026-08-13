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
    # Overridable: if the account cannot reach this model the P4 layer logs and
    # falls back to templates rather than failing the request.
    #
    # Not the mini tier. The guard catches invented ids and forbidden claims,
    # not reasoning that is wrong in the right vocabulary, and on the canonical
    # scenario the smaller model produced exactly that: it told an operator to
    # move a freight's ready time earlier, said an order had no approved
    # alternative while its window named one, and recommended a service that
    # misses the due date by six hours. Every fact it needed was in the payload.
    openai_model: str = "gpt-4o"

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
