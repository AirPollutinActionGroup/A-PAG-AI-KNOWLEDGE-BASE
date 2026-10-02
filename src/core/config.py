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

    # OCR — the fallback for a PDF page that has no text layer, because it is a photograph of a
    # page rather than a typed document. Off-by-default was considered and rejected: a scan that
    # silently becomes an unreadable document is the failure this exists to fix, and a setting
    # nobody turns on fixes nothing.
    OCR_ENABLED: bool = True
    # 144 dpi (pdfplumber renders at `resolution`). Enough for a 10pt government typeface;
    # doubling it roughly quadruples the pixels and the time for no measured accuracy gain.
    OCR_RESOLUTION: int = 144
    # Explicit, because the default is not a tuning detail: unset measured 20.5s per page against
    # 4.0s at 8 intra-op threads. 0 means min(8, cpu_count) rather than "let onnxruntime decide".
    OCR_THREADS: int = 0
    # A page with fewer than this many characters of real text is treated as unread. Not zero:
    # a scanned page often carries a stray character from a stamp or a page-number overlay.
    OCR_MIN_NATIVE_CHARS: int = 20
    # Below this, a line is more likely a signature, a stamp or a scan artifact than a word.
    # Dropping it leaves a gap; keeping it puts an invented word into a passage that will be
    # cited, and nothing downstream can tell a guessed word from a read one.
    OCR_MIN_CONFIDENCE: float = 0.5
    # A ceiling on how long one document can hold a worker: at ~4s a page, 500 pages is ~33
    # minutes. Pages past the cap are recorded as unread rather than quietly dropped.
    OCR_MAX_PAGES: int = 500

    # Reranking — read the candidates properly, then keep the best few.
    #
    # Hybrid retrieval is a recall device: both arms score a passage without ever looking at the
    # query and the passage together. A cross-encoder does, which is why it finds what fusion
    # ranked 16th — measured on this corpus, on three sample questions out of three the passage
    # that most directly answered the question sat *outside* the top 8 that hybrid alone would
    # have returned.
    RERANK_ENABLED: bool = True
    # Measured on an idle machine, 8 real questions, 50 candidates each:
    #   ms-marco-MiniLM-L-6-v2   1.28s median (1.07-2.13)   80MB
    #   jina-reranker-v1-turbo   1.62s median (1.47-3.09)  150MB
    # MiniLM-L-6 is both faster and half the size, so it is the default. Measure again before
    # changing it, and measure on an *idle* machine: a first pass taken while an OCR job held
    # eight cores reported 10.2s for this same model and ranked the two in the opposite order.
    RERANK_MODEL: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    # How many the hybrid arms fetch before reranking. The architecture says ~50, and this is
    # the latency dial: cross-encoder cost is linear in candidates, so halving this roughly
    # halves the 1.28s. Retrieval itself is 0.12s, so essentially all of a query's time is
    # here. Lowering it trades recall for speed — on this corpus the passage that best answered
    # a question sat at fused rank 9, 16 and 23, so a pool below ~25 would start losing the
    # answers this exists to find.
    RERANK_CANDIDATES: int = 50
    # Passages are truncated to this many characters before scoring. A cross-encoder's cost
    # scales with tokens, and a 2,000-character table contributes its relevance in the first
    # few hundred — the tail is rows, not subject matter. This bounds the worst case rather
    # than letting one long chunk set the latency of the whole query.
    RERANK_MAX_CHARS: int = 900

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
