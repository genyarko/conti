from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Anthropic ---
    anthropic_api_key: str = Field(default="", alias="ANTHROPIC_API_KEY")
    anthropic_model: str = Field(default="claude-opus-4-6", alias="ANTHROPIC_MODEL")
    anthropic_fast_model: str = Field(
        default="claude-haiku-4-5-20251001", alias="ANTHROPIC_FAST_MODEL"
    )
    anthropic_max_tokens: int = Field(default=4096, alias="ANTHROPIC_MAX_TOKENS")

    # --- Server ---
    engine_host: str = Field(default="0.0.0.0", alias="ENGINE_HOST")
    engine_port: int = Field(default=8000, alias="ENGINE_PORT")
    engine_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="ENGINE_LOG_LEVEL"
    )
    engine_env: Literal["development", "staging", "production"] = Field(
        default="development", alias="ENGINE_ENV"
    )

    # --- CORS ---
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "https://conti-nu.vercel.app",
            "http://localhost:5173",
            "http://localhost:3000",
        ],
        alias="CORS_ORIGINS",
    )

    # --- Rate limiting ---
    rate_limit_per_minute: int = Field(default=60, alias="RATE_LIMIT_PER_MINUTE")
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")

    # --- Response cache ---
    cache_enabled: bool = Field(default=True, alias="CACHE_ENABLED")
    cache_ttl_seconds: int = Field(default=900, alias="CACHE_TTL_SECONDS")
    cache_max_entries: int = Field(default=512, alias="CACHE_MAX_ENTRIES")

    # --- Request limits ---
    max_input_chars: int = Field(default=200_000, alias="MAX_INPUT_CHARS")
    max_claims_per_request: int = Field(default=200, alias="MAX_CLAIMS_PER_REQUEST")

    # --- Pipeline thresholds ---
    grounding_threshold_verified: int = Field(
        default=90, alias="GROUNDING_THRESHOLD_VERIFIED"
    )
    grounding_threshold_partial: int = Field(
        default=70, alias="GROUNDING_THRESHOLD_PARTIAL"
    )
    hallucination_grounding_max: int = Field(
        default=50, alias="HALLUCINATION_GROUNDING_MAX"
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json
                return json.loads(s)
            return [o.strip() for o in s.split(",") if o.strip()]
        return v

    @property
    def is_production(self) -> bool:
        return self.engine_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
