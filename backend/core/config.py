from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: Literal["development", "staging", "production"] = "development"
    app_log_level: str = "INFO"

    database_url: str = Field(
        default="postgresql+asyncpg://mentor:mentor@localhost:5432/ai_mentor"
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg://mentor:mentor@localhost:5432/ai_mentor"
    )

    jwt_secret: str = "change-me"
    jwt_algorithm: str = "HS256"
    jwt_access_token_ttl_min: int = 60

    ollama_base_url: str = "http://localhost:11434"
    ollama_gen_model: str = "qwen2.5:7b"
    ollama_embed_model: str = "nomic-embed-text"
    ollama_timeout_sec: int = 120
    ollama_temperature: float = 0.0

    pipeline_max_validation_retries: int = 2
    pipeline_cold_start_min_questions: int = 5
    pipeline_cold_start_min_attendance: int = 5

    severity_low_min: float = 0.75
    severity_medium_min: float = 0.50
    root_cause_low_attendance_pct: float = 0.75
    root_cause_weak_fundamentals_pct: float = 0.70

    vector_dim: int = 768

    # ----- Redis (caching + rate limiting + arq queue) -----------------
    # db 0: app cache, db 1: rate-limit counters, db 2: arq job queue.
    redis_url: str = "redis://redis:6379/0"
    redis_ratelimit_url: str = "redis://redis:6379/1"
    redis_arq_url: str = "redis://redis:6379/2"
    cache_enabled: bool = True
    cache_embedding_ttl_sec: int = 60 * 60 * 24 * 7   # embeddings are deterministic
    cache_retrieval_ttl_sec: int = 60 * 5              # retrieval results — short

    # ----- Rate limiting ----------------------------------------------
    rate_limit_enabled: bool = True
    rate_limit_default: str = "120/minute"   # catch-all per client IP
    rate_limit_login: str = "8/minute"       # brute-force guard on /auth/login
    rate_limit_chat: str = "20/minute"       # LLM cost guard
    rate_limit_ingest: str = "12/minute"     # embedding cost guard

    # ----- Circuit breakers -------------------------------------------
    breaker_fail_threshold: int = 5          # consecutive failures → OPEN
    breaker_recovery_sec: float = 30.0       # OPEN → HALF_OPEN after this
    breaker_half_open_max: int = 2           # trial calls allowed in HALF_OPEN

    # ----- Worker / autoscaling ---------------------------------------
    worker_max_jobs: int = 8
    autoscale_min_replicas: int = 2
    autoscale_max_replicas: int = 6

    # ----- Converter microservice & Dr.Doc model -----------------------
    converter_url: str = Field(default="http://converter:9000")
    dr_doc_model: str = "qwen2.5:3b"

    # ----- LMS bridge (Frappe ai_mentor app <-> this service) ----------
    # Inbound: Frappe POSTs /lms/events/* with this bearer.
    #   Mirrors Tenant Profile.ai_backend_secret on the Frappe side.
    lms_inbound_secret: str = Field(default="")
    # Outbound: this service POSTs back to Frappe webhook.ingest_* with this bearer.
    #   Mirrors Tenant Profile.ai_webhook_secret on the Frappe side.
    lms_outbound_secret: str = Field(default="")
    # Base URL of the Frappe site this backend belongs to (single-tenant per process).
    #   e.g. https://learn.acme.com
    lms_base_url: str = Field(default="")
    # Cosmetic — used in logs and to reject misrouted events.
    lms_tenant_code: str = Field(default="")
    lms_callback_timeout_sec: float = 8.0
    lms_callback_max_retries: int = 3


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
