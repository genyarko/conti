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

    # --- Google Gemini ---
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(
        default="gemini-3.1-pro-preview", alias="GEMINI_MODEL"
    )
    gemini_fast_model: str = Field(
        default="gemini-3-flash-preview", alias="GEMINI_FAST_MODEL"
    )
    gemini_max_tokens: int = Field(default=4096, alias="GEMINI_MAX_TOKENS")
    # Vertex AI / "Gemini Enterprise" path: bills through Cloud (uses trial
    # credit) instead of AI Studio's prepay pool. Auth comes from
    # Application Default Credentials (gcloud auth application-default login),
    # so no api_key is required when this flag is on.
    gemini_use_vertex: bool = Field(default=False, alias="GEMINI_USE_VERTEX")
    gemini_project: str = Field(default="", alias="GEMINI_PROJECT")
    # `global` is required for the 3.x preview models the codebase defaults to;
    # regional endpoints (us-central1, europe-west4, …) only serve GA models.
    gemini_location: str = Field(default="global", alias="GEMINI_LOCATION")
    # Service-account JSON (the contents of a key file). Production deploys
    # set this; locally we fall back to gcloud Application Default Credentials.
    google_credentials_json: str = Field(
        default="", alias="GOOGLE_APPLICATION_CREDENTIALS_JSON"
    )

    # --- Default provider/model (server-side safe default) ---
    # When a caller omits provider/model on /verify*, the engine resolves to
    # this pair. Gemini Flash is the cheapest safe option across providers.
    default_provider: Literal["anthropic", "google"] = Field(
        default="google", alias="DEFAULT_PROVIDER"
    )
    default_model: str = Field(
        default="gemini-3-flash-preview", alias="DEFAULT_MODEL"
    )

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

    # --- Auth ---
    # When set, all non-public data endpoints require `Authorization: Bearer <token>`.
    # Leave empty in dev to disable auth; required in production.
    api_auth_token: str = Field(default="", alias="API_AUTH_TOKEN")
    trusted_proxy_ips: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        alias="TRUSTED_PROXY_IPS",
        description=(
            "Client IPs of reverse proxies that are allowed to supply "
            "X-Forwarded-For. If empty, X-Forwarded-For is ignored."
        ),
    )
    trust_proxy_headers: bool = Field(
        default=False,
        alias="TRUST_PROXY_HEADERS",
        description=(
            "When true, trust X-Forwarded-For unconditionally — only safe on "
            "PaaS providers (e.g. Render) where every inbound request is "
            "guaranteed to traverse the platform proxy. Off by default."
        ),
    )

    # --- Rate limiting ---
    rate_limit_per_minute: int = Field(default=10, alias="RATE_LIMIT_PER_MINUTE")
    rate_limit_enabled: bool = Field(default=True, alias="RATE_LIMIT_ENABLED")

    # --- Response cache ---
    cache_enabled: bool = Field(default=True, alias="CACHE_ENABLED")
    cache_ttl_seconds: int = Field(default=900, alias="CACHE_TTL_SECONDS")
    cache_max_entries: int = Field(default=512, alias="CACHE_MAX_ENTRIES")

    # --- Request limits ---
    max_input_chars: int = Field(default=200_000, alias="MAX_INPUT_CHARS")
    max_claims_per_request: int = Field(default=200, alias="MAX_CLAIMS_PER_REQUEST")

    # --- Batch verification ---
    batch_max_items: int = Field(default=50, alias="BATCH_MAX_ITEMS")
    batch_concurrency: int = Field(default=8, alias="BATCH_CONCURRENCY")

    # --- Audit log (append-only JSONL, size-rotated) ---
    audit_enabled: bool = Field(default=True, alias="AUDIT_ENABLED")
    audit_path: str = Field(
        default=str(ROOT_DIR / "engine" / "logs" / "audit.jsonl"),
        alias="AUDIT_PATH",
    )
    audit_max_bytes: int = Field(default=10 * 1024 * 1024, alias="AUDIT_MAX_BYTES")
    audit_max_returned: int = Field(default=500, alias="AUDIT_MAX_RETURNED")

    # --- Explainability trace store (in-memory, same TTL as /audit/events context) ---
    trace_enabled: bool = Field(default=True, alias="TRACE_ENABLED")
    trace_ttl_seconds: int = Field(default=900, alias="TRACE_TTL_SECONDS")
    trace_max_entries: int = Field(default=256, alias="TRACE_MAX_ENTRIES")

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

    @field_validator("cors_origins", "trusted_proxy_ips", mode="before")
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
