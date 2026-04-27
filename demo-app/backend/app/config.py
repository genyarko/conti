from functools import lru_cache
from pathlib import Path
from typing import Literal

from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parents[1]


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
    anthropic_max_tokens: int = Field(default=16384, alias="ANTHROPIC_MAX_TOKENS")

    # --- Google Gemini ---
    gemini_api_key: str = Field(default="", alias="GEMINI_API_KEY")
    gemini_model: str = Field(
        default="gemini-3.1-pro-preview", alias="GEMINI_MODEL"
    )
    gemini_fast_model: str = Field(
        default="gemini-3-flash-preview", alias="GEMINI_FAST_MODEL"
    )
    gemini_max_tokens: int = Field(default=16384, alias="GEMINI_MAX_TOKENS")
    # Vertex AI / "Agent Platform" path: bills via Cloud project (uses trial
    # credit) instead of AI Studio prepay. Auth via Application Default
    # Credentials — no api_key needed when this flag is on.
    gemini_use_vertex: bool = Field(default=False, alias="GEMINI_USE_VERTEX")
    gemini_project: str = Field(default="", alias="GEMINI_PROJECT")
    # `global` is required for the 3.x preview models the codebase defaults to;
    # regional endpoints only serve GA models.
    gemini_location: str = Field(default="global", alias="GEMINI_LOCATION")
    # Service-account JSON (contents of a key file). Production deploys set
    # this; locally we fall back to gcloud Application Default Credentials.
    google_credentials_json: str = Field(
        default="", alias="GOOGLE_APPLICATION_CREDENTIALS_JSON"
    )

    # --- Default provider/model for the contract demo ---
    # Gemini Pro is the analyzer default because it can read PDF pages
    # multimodally (Phase G3). Override with DEFAULT_PROVIDER/DEFAULT_MODEL.
    default_provider: Literal["anthropic", "google"] = Field(
        default="google", alias="DEFAULT_PROVIDER"
    )
    default_model: str = Field(
        default="gemini-3.1-pro-preview", alias="DEFAULT_MODEL"
    )

    # --- Multimodal contract ingestion (Phase G3) ---
    # When the demo backend renders PDF pages to images for Gemini, cap the
    # per-page render DPI and the total number of pages we send.
    multimodal_enabled: bool = Field(default=True, alias="MULTIMODAL_ENABLED")
    multimodal_max_pages: int = Field(default=25, alias="MULTIMODAL_MAX_PAGES")
    multimodal_render_dpi: int = Field(default=144, alias="MULTIMODAL_RENDER_DPI")

    # --- TrustLayer engine ---
    trustlayer_base_url: str = Field(
        default="http://localhost:8000", alias="TRUSTLAYER_BASE_URL"
    )
    trustlayer_timeout_seconds: float = Field(
        default=120.0, alias="TRUSTLAYER_TIMEOUT_SECONDS"
    )
    # Token this backend presents when calling the engine upstream.
    trustlayer_api_token: str = Field(default="", alias="TRUSTLAYER_API_TOKEN")

    # --- Auth ---
    # Token this backend requires from clients on POST /upload, /analyze, and
    # /samples/{name}/load. Leave empty in dev to disable; required in prod.
    api_auth_token: str = Field(default="", alias="API_AUTH_TOKEN")

    # --- Server ---
    demo_host: str = Field(default="0.0.0.0", alias="DEMO_HOST")
    demo_port: int = Field(default=8100, alias="DEMO_PORT")
    demo_log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="DEMO_LOG_LEVEL"
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
            s = v.strip()
            if s.startswith("["):
                import json
                return json.loads(s)
            return [o.strip() for o in s.split(",") if o.strip()]
        return v


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
