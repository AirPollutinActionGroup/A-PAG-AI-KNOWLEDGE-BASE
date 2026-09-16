# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A-PAG AI Knowledge Base: an async document ingestion pipeline (Python 3.12, FastAPI) that will
eventually feed a governed RAG platform (vector search over PDFs) plus a Text-to-SQL layer over
PostgreSQL. **Currently implemented: Stages 1–5** (upload/quarantine → validation/threat scan →
promotion to raw storage → text extraction → normalization/quality gate), plus JWT auth,
multi-file upload, pagination, and full-text search on top of it. Classification, chunking,
vector indexing, and Text-to-SQL are not yet built (see Roadmap in README.md).

## Commands

```bash
# Environment
cp .env.example .env

# Start infra + API + worker (Postgres, MinIO, api, worker containers)
docker compose up -d

# Run DB migrations
alembic upgrade head

# Run app locally (outside docker)
python main.py            # FastAPI on :8000
python worker_main.py      # runs one stage, selected by WORKER_STAGE (default SCAN)
WORKER_STAGE=EXTRACT python worker_main.py
WORKER_STAGE=NORMALIZE python worker_main.py

# Tests
pytest tests/ -v                              # full suite (unit + Postgres integration)
pytest tests/unit -v                          # unit tests only (in-memory repo, no DB needed)
pytest tests/integration -v                   # integration tests (spins up a Postgres testcontainer, or falls back to DATABASE_URL if Docker is unavailable)
pytest tests/unit/test_ingestion_pipeline.py::TestName::test_case -v   # single test

# Lint
ruff check src/ tests/ main.py
```

Interactive endpoints once running: Swagger at `/docs`, health at `/health`, Studio UI at `/`
(note: Studio UI predates auth/multi-file and still expects a synchronous single-file response —
see `KNOWN_DEBTS.md`). All `/api/v1/documents/*` endpoints require `Authorization: Bearer <token>`
— get one via `POST /api/v1/auth/register` then `POST /api/v1/auth/login`.

## Architecture

### One API entrypoint, one worker entrypoint that runs any stage

- `main.py` → FastAPI app (`src/api/v1/router.py`) — handles `POST /documents/upload` (fast path
  only, returns `202` in <500ms) and `GET /documents/{id}/status`.
- `worker_main.py` → reads `WORKER_STAGE` (`SCAN` default) and runs the matching `BaseWorker`
  subclass — `ScanWorker`, `ExtractionWorker`, or `NormalizationWorker`. One process handles one
  stage; each stage runs as its own container in Docker Compose off the same image (see
  `docker-compose.yml`), because the container healthcheck watches a single heartbeat file, which
  assumes one worker daemon per container (`KNOWN_DEBTS.md` #5).

Processes never call into each other directly; they communicate only through the `documents` and
`jobs` tables in Postgres. When changing pipeline behavior, check both the fast-path call site
(`UploadService.receive()`, or the previous stage's handler enqueuing the next job) and the heavy
path (the stage's own `*JobHandler.process()`).

### Pipeline stages (by module)

1. **Receive/Quarantine** — `src/modules/document_pipeline/upload_service.py`
   `UploadService.receive()`: writes bytes to the `quarantine/` bucket, inserts a `Document` row
   (status `QUARANTINED`) and a `Job` row (`stage=SCAN`, `status=PENDING`) in the *same* request,
   then returns 202 immediately. Never runs validation itself.
2. **Claim** — `src/workers/base_worker.py` (`BaseWorker`): generic SKIP LOCKED job-queue engine
   (polling, atomic claim via `UPDATE ... WHERE ... FOR UPDATE SKIP LOCKED RETURNING`, lease
   expiry, exponential-backoff retries, a periodic reaper for stuck/expired leases, and a
   heartbeat file for container healthchecks). Each stage's worker (`ScanWorker`,
   `ExtractionWorker`, `NormalizationWorker`, in `src/workers/`) subclasses it and only implements
   `process_job()`, which opens a session and delegates to that stage's job handler. A new
   pipeline stage follows the same pattern: subclass `BaseWorker`, implement `process_job()`,
   raise `TransientProcessingError` vs `PermanentProcessingError` (`src/core/errors.py`) to
   control retry vs. immediate-fail behavior, and register the worker class in `worker_main.py`'s
   `WORKERS` dict.
3. **Validate/Scan/Promote** — `src/modules/document_pipeline/scan_job_handler.py`
   `ScanJobHandler.process()`: fetches the document, re-reads bytes from quarantine, runs
   `ValidationService` (`src/modules/document_pipeline/validation.py` — a fail-fast ladder: size,
   format lookup, container check, heuristic threat scan, deep structural check, SHA-256 — a
   decompression-bomb ratio check was tried and removed, see `KNOWN_DEBTS.md`), then either
   rejects (purges quarantine object, sets `REJECTED`) or promotes (copies to `raw/` bucket keyed
   by SHA-256, sets `VALIDATED`, enqueues an `EXTRACT` job). Dedup and promotion are guarded by an
   in-process lock (`self._promotion_lock`) plus a DB partial unique index
   (`uq_documents_active_sha256`) as the real guarantee under multi-worker concurrency. Handler
   methods are idempotent — re-running `process()` on an already-finalized (or already-promoted)
   document is a safe no-op that returns the existing state (important since jobs can be
   retried/reaped).
4. **Extract** — `src/modules/document_pipeline/extraction_job_handler.py`
   `ExtractionJobHandler.process()`: fetches a `VALIDATED` document, reads its `raw/` bytes, and
   dispatches to the `TextExtractor` registered for its MIME type
   (`src/modules/document_pipeline/extraction/extractors.py` — one per format, deliberately
   non-ML; see the "Text extraction" section below). Writes the result to
   `extracted/{document_id}.json`, sets `EXTRACTED`, enqueues a `NORMALIZE` job. A file that can't
   be parsed sets `EXTRACTION_FAILED` and stops the pipeline there — no job is queued after a
   failure at any stage.
5. **Normalize** — `src/modules/document_pipeline/normalization_job_handler.py`
   `NormalizationJobHandler.process()`: fetches an `EXTRACTED` document, reads its
   `extracted/{id}.json`, and runs `NormalizationService` (`src/modules/document_pipeline/normalization/`
   — text cleaning, language detection, then a quality gate). This stage never opens the original
   file and has no per-format branching — structure (headings/tables) is carried through from
   extraction, not re-derived. A passing document is written to `normalized/{id}.json` and set to
   `AWAITING_CLASSIFICATION` — which now means what it says (normalization succeeded), not "just
   promoted" as it did before this stage existed. A quality-gate failure sets
   `NORMALIZATION_FAILED` and is never retried (the content was read correctly; retrying produces
   the identical verdict). Classification (Stage 6+) isn't built yet, so nothing is queued after
   this stage.

### Text extraction: deliberately non-ML

`src/modules/document_pipeline/extraction/extractors.py` reads what each format already states
rather than inferring it: Word paragraph styles are headings, slide titles are headings, worksheets
are already grids. PDF is the only format with no semantic structure, so it gets pdfplumber's
ruling-line table detection plus a font-size heading heuristic (weighted by character count, not
line count — see the code comment on why a line-count tie can eat a document's only heading).
Docling (the design docs' choice, and still current best practice for self-hosted layout parsing)
was evaluated and rejected for now: it resolves to 78 packages including `torch`/`transformers`/
`opencv-python` for capabilities — OCR, multi-column layout inference — this corpus doesn't need,
since A-PAG's documents are typed, not scanned. See `KNOWN_DEBTS.md` #13 before reintroducing it;
the `TextExtractor` ABC and `EXTRACTORS` registry exist so swapping one format's extractor is a
one-line registry change, not a pipeline rewrite.

**No OCR** (`KNOWN_DEBTS.md` #14) is the corresponding decision on the input side — this corpus has
no scanned/photographed documents, so an OCR fallback would be dead code. The safety net for that
assumption being wrong is the normalization quality gate's `LOW_TEXT_DENSITY`/`EMPTY_TEXT` checks
(`src/modules/document_pipeline/normalization/quality_checker.py`), scoped to PDF since it's the
only format that can be a scan — a sparse `.pptx` or `.xlsx` is a legitimate document, not a
failed extraction.

### Storage: 4-bucket + repository abstraction

- `src/storage/object_storage.py` defines `ObjectStorage` (abstract) with `LocalFileSystemStorage`
  (dev, writes under `./storage_data/`) and `MinIOStorage` (prod) implementations, selected via
  `BucketManager` per `settings.STORAGE_BACKEND`. Buckets: `quarantine/` (untrusted, purged after
  promotion or rejection) → `raw/` (validated bytes, permanent) → `extracted/` (per-document
  `{id}.json`, the raw extraction result) → `normalized/` (per-document `{id}.json`, cleaned +
  quality-gated — the artifact a future chunking stage will read). `extracted/`/`normalized/` keys
  are deterministic (`storage_keys.py`'s `extraction_key_for`/`normalized_key_for`), not
  content-addressed like `raw/` — two documents with identical bytes were already deduplicated at
  promotion, so keying by document_id needs no lookup for a handler to find its own output.
- `src/modules/document_pipeline/repository.py` defines `DocumentRepository` (abstract) with
  `PostgreSQLDocumentRepository` (production, wraps a SQLAlchemy `Session`) and
  `InMemoryDocumentRepository` (unit tests / concurrency tests, no DB needed). Both `UploadService`
  and `ScanJobHandler` take a repository via constructor injection — prefer adding new methods to
  the abstract base and both implementations together, not special-casing one.

### Audit trail

`src/modules/audit/service.py` (`AuditService.log_event`) writes append-only rows to `audit_log`
(DB-level triggers forbid UPDATE/DELETE — migration `0003_revoke_audit_log_writes.py`). Every
state transition should call `self._audit(...)` (both `UploadService` and `ScanJobHandler` have a
private `_audit()` wrapper) with a `correlation_id` threaded through the whole request/job
lifecycle. Audit writes are **best-effort, not transactional** with the state change — a failure is
logged at ERROR but never blocks the pipeline (deliberate trade-off, see `KNOWN_DEBTS.md` #1). The
worker-side `_audit()` additionally de-dupes by checking for an existing `(document_id,
event_type)` row first, since jobs can be retried.

### Auth & access control

- `src/modules/auth/` — `security.py` hashes passwords with `bcrypt` directly (not `passlib`,
  which is unmaintained and incompatible with `bcrypt>=4.1` — see `ARCHITECTURE.md` §6a) and
  issues/decodes JWTs; `service.py` (`AuthService`) does registration/authentication against the
  `users` ORM table directly (no repository abstraction — see its module docstring for why);
  `dependencies.py::get_current_user` is the **single seam** every endpoint depends on for "who is
  calling this" — when SSO replaces JWT login later, only this function's internals change.
- `src/api/v1/auth.py` exposes `/auth/register`, `/auth/login` (OAuth2 password flow: form fields
  `username`/`password`), `/auth/me`.
- Access model: `Classification.PUBLIC` (org-wide) vs `RESTRICTED` (owner-scoped: visible to
  `doc.owner_id == current_user.user_id`, plus any `ADMIN`) — enforced by `_can_view()` in
  `src/api/v1/ingestion.py`, applied uniformly across status/list/search. Deliberately a 2-tier
  model, not per-document ACLs — see `ARCHITECTURE.md` §6b before adding per-document or
  department-based sharing (a prior department-tier attempt shipped without departments ever
  being populated, making RESTRICTED admin-only in practice — see `KNOWN_DEBTS.md`).
- Rate limiting: `slowapi` on `POST /documents/upload` (`settings.UPLOAD_RATE_LIMIT`), wired via
  `app.state.limiter` in `src/api/v1/router.py`.

### Supported formats

`src/modules/document_pipeline/formats.py` is the single registry of accepted formats (PDF, DOCX,
XLSX, PPTX). **Adding a format means adding one `FormatSpec` to `FORMATS`, not editing
`validation.py`** — the validator owns the shared ladder, the spec owns everything format-specific
(extension, magic bytes, identifying zip part, unit counting, and the two check callables).
`storage_keys.py` derives object-key extensions from the same registry, so there is one source of
truth rather than a second map to keep in sync.

Two structural checks exist, not four: PDF has its own binary layout, while DOCX/XLSX/PPTX are all
Office Open XML — identical ZIP containers differing only by which XML part they carry. The three
OOXML specs therefore share one pair of check functions and differ only by data.

Each spec carries **two** callables, and the split is load-bearing: `container_check` (cheap, no
parsing) runs *before* the threat scan so a disguised binary is reported as corrupt, and
`structural_check` (deep parse, produces the unit count) runs *after* it so a file carrying a known
malicious signature is reported as malicious rather than as whatever the parser chokes on first.
Collapsing these into one call silently reclassifies malicious uploads — there is a regression test
for this (`test_reject_structural_threats`).

Format is resolved from file **content** via `detect_format()` at the API boundary, not from the
declared MIME type (browsers send `application/octet-stream` for valid Office files), and the
worker independently re-verifies content against the stored type before promoting.

`page_count` is really a per-format "unit count": pages for PDF, worksheets for XLSX, slides for
PPTX, and `None` for DOCX — Word text reflows, so a page count doesn't exist until the document is
rendered, and faking one from `app.xml` would be wrong. The column is nullable for this reason.

### Config & enums

- All runtime config is centralized in `src/core/config.py` (`Settings`, pydantic-settings, reads
  `.env`). Add new tunables here rather than reading `os.environ` directly.
- All lifecycle/status/event vocab lives in `src/db/enums.py` (`DocumentStatus`, `Classification`,
  `JobStage`, `JobStatus`, `AuditEventType`) — string enums shared between the DTOs
  (`src/modules/document_pipeline/models.py`), the ORM (`src/db/models.py`), and Postgres columns.
  Extend enums here rather than introducing new string literals.

### DB migrations

Alembic migrations live in `src/db/migrations/versions/`, numbered sequentially. Notable ones:
`0003` revokes UPDATE/DELETE on `audit_log` at the DB level (immutability enforced by the
database, not just application code); `0004` adds the partial unique index on
`documents(sha256)` excluding `SUPERSEDED/ARCHIVED/REJECTED` (the real dedup guarantee, since
application-level checks alone race under concurrency); `0006_users_departments` adds `users` and
document ownership/metadata columns (`title`, `description`, `mime_type`, `page_count`,
`upload_batch_id`, `deleted_at`), a real FK on `uploader_user_id`, and a trigger-maintained
`search_vector` (tsvector) for full-text search — its `upgrade()` checks for existing
tables/columns/constraints first, since integration test fixtures calling
`Base.metadata.create_all()` can otherwise race ahead of Alembic in dev. `0006` also added a
`departments` table and `department_id` columns; `0007_owner_scoped_access` drops all of that
plus `documents.doc_type` — departments were never populated, which made department-scoped
RESTRICTED visibility effectively admin-only, and `doc_type` (a content category) can't be
derived from a file and nothing read it. Access control is now owner-scoped — see
`ARCHITECTURE.md` §6b.

`0011_extract_normalize_statuses` widens `chk_documents_status` (adds `EXTRACTED`,
`EXTRACTION_FAILED`, `NORMALIZATION_FAILED`) and `chk_audit_log_event_type` (adds the matching
`EXTRACTION_*`/`NORMALIZATION_*` event types) for Stages 4–5. It repurposes the existing
`VALIDATED` status rather than adding a new one — defined since `0002`, never assigned by any
code before this.

**Revision id length**: `alembic_version.version_num` defaults to `VARCHAR(32)` — keep every new
revision id ≤32 characters, or the final version-bump statement fails and rolls back the entire
migration transaction (this happened during `0006`'s development, and again with
`0011_extract_normalize_statuses`, whose first attempt at a fully descriptive id was 38
characters).

### Testing conventions

- `tests/unit/` — use `InMemoryDocumentRepository`, no DB/Docker required; exercise pipeline logic
  and worker mechanics directly.
- `tests/integration/` — real PostgreSQL via `testcontainers` (`tests/integration/conftest.py`;
  the package must be installed — `uv sync` covers this, since it's declared in `pyproject.toml`,
  not just `requirements.txt`). Tests run inside a rolled-back transaction per test (`db_session`
  fixture), but some worker tests (`test_worker_execution.py`, `test_worker_skip_locked.py`)
  `DELETE` the whole `documents`/`jobs` tables outside that transaction to get a clean queue —
  harmless in a disposable container, destructive against a real database. If `testcontainers`
  can't start a container, the suite refuses to fall back to `settings.DATABASE_URL` silently —
  it `pytest.skip()`s with a fix-it message unless `APAG_ALLOW_DESTRUCTIVE_DB_TESTS=1` is set.
  Never set that env var against a database with real data.
- `tests/fixtures/pdfs/` contains real PDF fixtures (valid, corrupted header, truncated EOF,
  disguised binaries, embedded-script malware samples, password-protected) exercised through
  named presets in `GET /documents/test-preset/{preset_name}` for the Studio UI — when adding a new
  validation check, add a matching fixture + preset rather than only unit-testing it in isolation.

## Key trade-offs to know before changing things

These are documented, deliberate decisions — see `ARCHITECTURE.md` and `KNOWN_DEBTS.md` for full
rationale before "fixing" them:

- Workers use **synchronous** SQLAlchemy sessions, not `asyncpg`/async ORM (CPU/IO-bound work gets
  no benefit from asyncio; keeps FastAPI's event loop unstarved).
- Job orchestration is **Postgres `SKIP LOCKED`**, not Kafka/RabbitMQ/Redis — deliberate, scoped to
  A-PAG's expected 50–500 user scale.
- Malware scanning (`ClamAVScanner` in `validation.py`) is a **heuristic signature check only**
  (`/JavaScript`, `/Launch`, `/OpenAction`, EICAR string), not real ClamAV/`clamd`. This is a
  deliberate deferral based on the actual threat model (trusted internal uploaders, PDFs are never
  rendered/executed by this system) — see `KNOWN_DEBTS.md` #8 for the specific triggers that would
  require wiring in real `clamd`. Don't "fix" this without reading that rationale first.
- Idempotency (duplicate job execution, duplicate audit events) is enforced in **application
  logic**, not DB constraints — see `KNOWN_DEBTS.md` #3 before assuming a DB-level guard exists.
  The one exception: concurrent dedup promotion races on `uq_documents_active_sha256` are caught
  as an `IntegrityError` in `ScanJobHandler.process()` and routed to `DUPLICATE` — that DB
  constraint is the real guarantee, application code is just handling its failure mode.
- Auth is **JWT + bcrypt against local Postgres**, not SSO — deliberate for the current
  50-employee, no-existing-SSO phase. See `ARCHITECTURE.md` §6a.
- Registration is **open** (`POST /auth/register` has no invite/admin gate) — deliberate only
  while the API is internal-only. See `KNOWN_DEBTS.md` #0.
- Text extraction is **non-ML** (`pdfplumber`/`python-docx`/`python-pptx`/`openpyxl`), not Docling
  — deliberate given this corpus is typed documents whose structure the file already states, not
  scanned/complex-layout PDFs Docling's layout inference exists for. See `KNOWN_DEBTS.md` #13
  before adding it back; the `TextExtractor` registry is designed for a one-format swap, not a
  wholesale rewrite, if a real document proves the trade-off wrong.
- There is **no OCR** — deliberate for the same reason (no scanned documents expected), with the
  normalization quality gate's `LOW_TEXT_DENSITY` check as the explicit safety net rather than a
  silent assumption. See `KNOWN_DEBTS.md` #14 for the trigger to revisit.
