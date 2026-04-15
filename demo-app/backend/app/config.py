from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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

    # --- TrustLayer engine ---
    trustlayer_base_url: str = Field(
        default="http://localhost:8000", alias="TRUSTLAYER_BASE_URL"
    )
    trustlayer_timeout_seconds: float = Field(
        default=120.0, alias="TRUSTLAYER_TIMEOUT_SECONDS"
    )

    # --- Server ---
    demo_host: str = Field(default="0.0.0.0", alias="DEMO_HOST")
    demo_port: int = Field(default=8100, alias="DEMO_PORT")
    demo_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="DEMO_LOG_LEVEL"
    )

    # --- CORS ---
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"],
        alias="CORS_ORIGINS",
    )

    # --- Upload limits ---
    max_upload_bytes: int = Field(default=10 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")
    max_contract_chars: int = Field(default=200_000, alias="MAX_CONTRACT_CHARS")

    # --- Analysis ---
    max_findings_verified_in_parallel: int = Field(
        default=6, alias="MAX_FINDINGS_VERIFIED_IN_PARALLEL"
    )

    sample_contracts_dir: Path = ROOT_DIR / "sample_contracts"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
