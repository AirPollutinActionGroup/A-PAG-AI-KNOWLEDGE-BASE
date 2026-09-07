"""Core application configuration using Pydantic BaseSettings."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    PROJECT_NAME: str = "A-PAG AI Knowledge Base"
    ENVIRONMENT: str = "development"

    # Database
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "apag_knowledge_base"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "apag_secure_password_2026"
    DATABASE_URL: str = (
        "postgresql://postgres:apag_secure_password_2026@localhost:5432/apag_knowledge_base"
    )

    # Storage
    STORAGE_BACKEND: str = "local"
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "apag_admin"
    MINIO_SECRET_KEY: str = "apag_secure_password_2026"
    MINIO_SECURE: bool = False

    # Worker Configuration
    WORKER_POLL_INTERVAL_SECONDS: float = 1.0
    SCAN_WORKER_LEASE_SECONDS: int = 60
    WORKER_HEARTBEAT_FILE: str = "/tmp/worker_alive"
    MAX_JOB_RETRIES: int = 3
    RETRY_BACKOFF_BASE_SECONDS: int = 5
    RETRY_BACKOFF_MAX_SECONDS: int = 60
    REAPER_INTERVAL_SECONDS: int = 30

    # Auth (JWT)
    # NOTE: JWT_SECRET_KEY has an insecure default so local/dev works out of the box.
    # It MUST be overridden via .env (a long random string) before any shared/production deploy.
    JWT_SECRET_KEY: str = "dev-only-insecure-secret-change-me-in-env-file"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 480  # 8h workday

    # Upload limits
    MAX_FILES_PER_UPLOAD: int = 5
    MAX_BATCH_SIZE_BYTES: int = 250 * 1024 * 1024  # 250 MB
    UPLOAD_RATE_LIMIT: str = "20/hour"

    # Registration is restricted to this email domain (see AuthService.register)
    ALLOWED_EMAIL_DOMAIN: str = "a-pag.org"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
