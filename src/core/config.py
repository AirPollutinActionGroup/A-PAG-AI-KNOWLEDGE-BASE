"""Core application configuration using Pydantic BaseSettings."""

from pydantic import field_validator
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
    # Bounds how much memory one extraction may demand, not how large a file may be. pdfplumber
    # holds page objects for the document it parses; a real 1,351-page PDF peaked at 3.6GB RSS.
    # Deployments with less RAM must lower this — an OOM kill mid-job is re-queued by the reaper
    # and kills the next worker on the same document. See KNOWN_DEBTS.md #24.
    MAX_PDF_PAGES: int = 5000

    MAX_FILES_PER_UPLOAD: int = 5
    MAX_BATCH_SIZE_BYTES: int = 250 * 1024 * 1024  # 250 MB
    UPLOAD_RATE_LIMIT: str = "20/hour"

    # Registration is restricted to this email domain (see AuthService.register)
    ALLOWED_EMAIL_DOMAIN: str = "a-pag.org"

    # Normalization quality gate.
    # MIN_PDF_CHARS_PER_PAGE is the safety net for the deliberate decision not to run OCR: a
    # scanned page has no text layer and extracts to roughly nothing, so a PDF averaging fewer
    # characters than this per page is flagged rather than indexed as near-empty content. Scoped
    # to PDF because it is the only format that can be a scan — a sparse .pptx is a legitimate
    # visual deck, not a failed extraction. Set low enough that a genuinely sparse text PDF
    # (title pages, chart-heavy reports) passes; a real page of prose runs to thousands.
    MIN_PDF_CHARS_PER_PAGE: int = 50

    # Chunking.
    # Sized in characters rather than tokens: the tokenizer belongs to the embedding model, which
    # is a stage later, and guessing with a foreign tokenizer is worse than an honest character
    # budget. CHUNK_TARGET_CHARS is what the packer aims for; CHUNK_MAX_CHARS is the hard ceiling
    # that forces a split. ~1600 characters is roughly 400 English tokens — deliberately well
    # under a 512-token window, because Devanagari runs 2-3x more tokens per character and a
    # budget tuned on English would silently truncate Hindi documents at embed time.
    CHUNK_TARGET_CHARS: int = 1600
    CHUNK_MAX_CHARS: int = 2000

    # Token budget, used when the chunking stage has a tokenizer. This is the real constraint —
    # the character figures above are a proxy that held only while text stayed near the corpus
    # average, and 2.9% of the corpus was silently truncated where it did not (KNOWN_DEBTS.md
    # #28). The effective maximum is min(CHUNK_MAX_TOKENS, model window), so a model with a
    # larger window does not silently produce enormous chunks: precision falls as one passage
    # covers more distinct ideas, whatever the model can ingest.
    #
    # 500 rather than 512 leaves headroom so a boundary case cannot land exactly on the limit.
    CHUNK_MAX_TOKENS: int = 500
    CHUNK_TARGET_TOKENS: int = 400

    # Below this cosine similarity, retrieval reports that it found nothing rather than returning
    # its least-bad guess. Measured on this corpus with bge-base-en-v1.5: on-topic questions score
    # 0.69-0.84 against their best passage, plainly off-topic ones 0.45-0.50. 0.55 sits in that
    # gap, leaning permissive — a weak result the reader can judge from its citation is better
    # than a refusal on a question the corpus does answer.
    #
    # Model-specific. Every embedding model has its own similarity distribution, so this needs
    # re-measuring on a model change, not carrying over.
    SEARCH_MIN_SIMILARITY: float = 0.55

    # ---- Answer generation (Sarvam) -------------------------------------------------------
    # The one place text leaves the deployment. Sarvam was chosen over OpenAI/Anthropic because
    # it is an Indian provider with data staying in India, which is the nearest thing to the
    # sovereignty the rest of this system has by construction.
    #
    # Sarvam-M and Sarvam-30B were both deprecated within a year of release, so treat the model
    # name as something that will change again and not as a constant.
    SARVAM_API_KEY: str = ""
    SARVAM_MODEL: str = "sarvam-105b"
    SARVAM_TIMEOUT_SECONDS: float = 60.0

    # Low, not zero: the task is extraction and summary over supplied text, where invention is
    # the failure mode and sampling variety buys nothing.
    GENERATION_TEMPERATURE: float = 0.2
    GENERATION_MAX_TOKENS: int = 1024

    # How many retrieved passages are sent. More context is not free — it costs money per token,
    # dilutes the model's attention, and past a point lowers answer quality rather than raising
    # it. Ten passages at ~200 tokens each is roughly 2,000 tokens of context.
    GENERATION_MAX_PASSAGES: int = 8

    # Whether RESTRICTED documents may be sent to the API. The retrieval layer has already
    # decided the *caller* may see them; this is the separate question of whether they may leave
    # the network.
    #
    # **False, because the architecture requires it.** Agent 0101 §3 states that external
    # inference is for non-restricted content only and that the gateway "refuses to send
    # restricted material at all"; §5 adds that a request takes the highest tier present across
    # every passage, with no averaging — seven public passages and one restricted one is a
    # restricted request. This defaulted to true in its first draft, which contradicted the
    # security keystone of the design. Data residency in India is not the same guarantee as
    # never leaving the building.
    GENERATION_INCLUDE_RESTRICTED: bool = False

    # Embedding.
    # EMBEDDING_DIMENSIONS must match the migrated vector(N) column. It is not a tuning knob:
    # changing it requires a migration and a full re-embed of the corpus, so FastEmbedProvider
    # probes the model at load and refuses to start on a mismatch rather than failing per-chunk
    # deep inside a worker.
    EMBEDDING_MODEL: str = "BAAI/bge-base-en-v1.5"
    EMBEDDING_DIMENSIONS: int = 768
    EMBEDDING_BATCH_SIZE: int = 32
    # An English-only model turns Devanagari into unknown tokens and emits vectors that match
    # nothing, so such a document would sit in the index invisible with no signal it is missing.
    # Skipping records the gap instead. Set False when a multilingual model is configured.
    EMBEDDING_SKIP_NON_ENGLISH: bool = True

    @field_validator("DATABASE_URL")
    @classmethod
    def _pin_postgres_driver(cls, value: str) -> str:
        """Names the driver explicitly instead of trusting SQLAlchemy's default for
        `postgresql://`.

        That default changed in SQLAlchemy 2.1: bare `postgresql://` now resolves to psycopg v3
        rather than psycopg2. This project installs psycopg2-binary, so an unpinned SQLAlchemy
        upgrade turned every container into a crash loop with `No module named 'psycopg'` —
        despite psycopg2 being present and the code being unchanged.

        Normalising here rather than in every .env, compose file and CI config means the URL can
        keep its conventional form everywhere and still resolve to the driver that is actually
        installed.
        """
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+psycopg2://", 1)
        return value

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
