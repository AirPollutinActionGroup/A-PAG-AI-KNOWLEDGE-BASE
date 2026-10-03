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
    # Sarvam exposes two models and they behave completely differently, despite the shared
    # name. `sarvam-105b` is a **reasoning** model: against a real eight-passage context it
    # spent 6,229 completion tokens and 75 seconds writing 17,000 characters of chain-of-thought
    # to produce a 932-character answer — and at 4 passages it exhausted an 8,192-token budget
    # and returned nothing at all. `sarvam-105b-conversations` does no reasoning:
    #
    #   sarvam-105b                  8 passages   75.4s   6,229 tokens   Rs 0.584
    #   sarvam-105b-conversations    8 passages    1.6s     251 tokens   Rs 0.147
    #
    # 47x faster and 4x cheaper for an answer of the same quality, both correctly cited.
    # Reasoning buys nothing here: the task is extraction from supplied passages, and the model
    # was deliberating over text it had already been given.
    SARVAM_MODEL: str = "sarvam-105b-conversations"
    # Generous relative to the 1.6s the conversations model takes, because the failure this
    # guards against is a hung connection, not a slow answer — and the reasoning model, if
    # anyone configures it, needs well over 60.
    SARVAM_TIMEOUT_SECONDS: float = 120.0

    # Low, not zero: the task is extraction and summary over supplied text, where invention is
    # the failure mode and sampling variety buys nothing.
    GENERATION_TEMPERATURE: float = 0.2

    # sarvam-105b is a **reasoning model**: it writes a chain of thought into
    # `reasoning_content` and only then writes the answer into `content`. Both are billed as
    # completion tokens, and the reasoning is far the larger of the two.
    #
    # Measured on one short question, identical answer both times:
    #   default effort      2,682 completion tokens  ->  "[1] 36 months."
    #   reasoning_effort=low   419 completion tokens  ->  "[1] 36 months"
    #
    # 6.4x the cost for the same sentence. This task is extraction from supplied passages, not
    # a problem that rewards deliberation, so "low" is the default. Sarvam accepts only 'low',
    # 'medium' or 'high' — reasoning cannot be turned off, and `thinking: false` is ignored.
    GENERATION_REASONING_EFFORT: str = "low"

    # Must cover the reasoning *and* the answer, because the reasoning is spent first. At 300
    # the model used the entire budget thinking and returned `content: null` with
    # finish_reason "length" — an empty answer with a full token bill. 2048 leaves headroom at
    # low effort; raise it together with GENERATION_REASONING_EFFORT, never separately.
    GENERATION_MAX_TOKENS: int = 2048

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

    # Whether the gateway scans and masks sensitive values before anything is sent.
    #
    # On by default and intended to stay on: the point of a boundary is that it cannot be
    # bypassed, and a control that ships off is a control nobody has tested. The switch exists
    # for diagnosing a recogniser that is firing wrongly on a specific corpus, not as a
    # deployment choice.
    GATEWAY_REDACT: bool = True

    # Evaluation only. The judge for `run_ragas.py`, which grades answer quality offline.
    #
    # Deliberately a different provider from the one under test: using Sarvam to grade Sarvam's
    # answers measures self-consistency, not truthfulness. It is **not** a second route for the
    # application -- nothing in `src/` reads these except the harness, and the harness applies
    # the same classification filter and redaction the gateway does before any passage reaches
    # OpenAI.
    OPENAI_API_KEY: str = ""
    RAGAS_JUDGE_MODEL: str = "gpt-5-mini"

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

    # How much of a document must be running prose before its detected language is believed.
    #
    # Language detection needs sentences. Given a grid it answers anyway, and confidently: a
    # 130,000-character emissions spreadsheet — `em  country  units  X2000  X2001 ...` — was
    # detected as **Croatian** and skipped, taking 460 chunks out of the index with no signal
    # beyond a status nobody was looking at.
    #
    # Measured on this corpus, the separation is wide: that file scores 0.054, while real
    # documents score 0.53-0.72. Deliberately *not* a rule about spreadsheets — another .xlsx
    # here scores 0.723 and is detected correctly, so excluding the format would have been both
    # wrong and a coincidence that happened to work.
    #
    # Below this, the detection is treated as unknown rather than as non-English, and the
    # document is embedded. The downside is bounded: a genuinely Devanagari *table* would be
    # embedded as noise. The upside is that a column of English plant names stays searchable.
    EMBEDDING_MIN_PROSE_RATIO: float = 0.15

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
    #
    # Raised from 0.5 after reading a live citation. Three lines of noise survived into the
    # Ministry of Power memorandum's first page and were shown to a reader:
    #
    #     0.65  'I r ns  sn    d  res t dy'
    #     0.67  'o.in o n nn nn n i n nc'
    #     0.74  'Ppoit i  i i     i  nes'
    #
    # while every genuine line on that page scored 0.97 or better. The margin is wide enough
    # that 0.80 removes all three and costs nothing real -- but it is corpus-specific, so
    # re-measure on a scan of different quality rather than assuming it carries over.
    OCR_MIN_CONFIDENCE: float = 0.80
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
