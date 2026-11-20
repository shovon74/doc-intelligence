from typing import List, Literal, Optional
from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


_DEFAULT_SECRET = "super-secret-docintel-key-change-in-production-32chars"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Environment & Logging
    ENVIRONMENT: str = "development"
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173,http://localhost:8080"

    # Database & Redis
    DATABASE_URL: str = "postgresql+psycopg://app:app@postgres:5432/docintel"
    REDIS_URL: str = "redis://redis:6379/0"

    # JWT Security
    JWT_SECRET: str = _DEFAULT_SECRET
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 1440

    # Storage & Upload Limits
    MAX_UPLOAD_MB: int = 100
    UPLOAD_DIR: str = "/data/uploads"

    # Resource limits (reject abusive files before they exhaust memory/CPU)
    MAX_PAGES: int = 1000                  # PDF pages / TIFF frames / spreadsheet sheets
    MAX_IMAGE_PIXELS: int = 100_000_000    # decompression-bomb guard (width x height)
    MAX_SHEET_ROWS: int = 200_000          # rows per spreadsheet sheet

    # Large-PDF batching and task retries
    PAGE_BATCH_SIZE: int = 20              # pages per Celery batch task / DB flush
    TASK_MAX_RETRIES: int = 3              # retries for retryable errors only
    TASK_RETRY_BACKOFF_SECONDS: int = 5    # base delay; doubles each retry, capped at 300 s

    # LLM Providers
    OPENAI_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None

    LLM_PROVIDER: Literal["auto", "openai", "gemini", "fake"] = "gemini"
    LLM_PREFERENCE: str = "gemini,openai"
    LLM_FALLBACK_ENABLED: bool = True

    OPENAI_MODEL: str = "gpt-4o-mini"
    GEMINI_MODEL: str = "gemini-2.5-flash"
    LLM_TIMEOUT_SECONDS: int = 60
    LLM_MAX_RETRIES: int = 3

    # Embeddings
    EMBEDDING_PROVIDER: Literal["auto", "openai", "gemini", "fake"] = "gemini"
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    GEMINI_EMBEDDING_MODEL: str = "gemini-embedding-001"
    EMBEDDING_DIM: int = 768

    # OCR / PDF Pipeline
    OCR_LANGS: str = "eng"
    OCR_DPI: int = 250
    PDF_MIN_TEXT_CHARS_PER_PAGE: int = 50
    USE_LLM_VISION_FALLBACK: bool = False

    @field_validator("JWT_SECRET")
    @classmethod
    def _secret_not_blank(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("JWT_SECRET must be at least 32 characters")
        return v

    @model_validator(mode="after")
    def _production_needs_real_secret(self) -> "Settings":
        if self.ENVIRONMENT.lower() == "production" and self.JWT_SECRET == _DEFAULT_SECRET:
            raise ValueError("Set JWT_SECRET to a private value when ENVIRONMENT=production")
        return self

    @property
    def cors_origins_list(self) -> List[str]:
        return [origin.strip() for origin in self.CORS_ORIGINS.split(",") if origin.strip()]

    @property
    def has_openai(self) -> bool:
        return bool(self.OPENAI_API_KEY and self.OPENAI_API_KEY.strip())

    @property
    def has_gemini(self) -> bool:
        return bool(self.GEMINI_API_KEY and self.GEMINI_API_KEY.strip())

    @property
    def configured_providers(self) -> List[str]:
        providers = []
        if self.has_openai:
            providers.append("openai")
        if self.has_gemini:
            providers.append("gemini")
        return providers


settings = Settings()
