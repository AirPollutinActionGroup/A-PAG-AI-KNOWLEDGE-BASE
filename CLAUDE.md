# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A-PAG AI Knowledge Base: an async document ingestion pipeline (Python 3.12, FastAPI) that will
eventually feed a governed RAG platform (vector search over PDFs) plus a Text-to-SQL layer over
PostgreSQL. **Currently implemented: Stages 1–3 only** (upload/quarantine → validation/threat scan →
promotion to raw storage + audit trail), plus JWT auth, multi-file upload, pagination, and
full-text search on top of it. Text extraction, OCR, chunking, vector indexing, and Text-to-SQL are
not yet built (see Roadmap in README.md).

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
python worker_main.py      # ScanWorker daemon

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

### Two entrypoints, one shared pipeline

- `main.py` → FastAPI app (`src/api/v1/router.py`) — handles `POST /documents/upload` (fast path
  only, returns `202` in <500ms) and `GET /documents/{id}/status`.
- `worker_main.py` → `ScanWorker` daemon — does all the actual heavy lifting (validation, threat
  scan, dedup, promotion) picked up from a DB-backed job queue.

These two processes never call into each other directly; they communicate only through the
`documents` and `jobs` tables in Postgres. When changing pipeline behavior, check both call sites:
`UploadService.receive()` (API, fast path) and `ScanJobHandler.process()` (worker, heavy path).

### Pipeline stages (by module)

1. **Receive/Quarantine** — `src/modules/document_pipeline/upload_service.py`
   `UploadService.receive()`: writes bytes to the `quarantine/` bucket, inserts a `Document` row
   (status `QUARANTINED`) and a `Job` row (`stage=SCAN`, `status=PENDING`) in the *same* request,
   then returns 202 immediately. Never runs validation itself.
2. **Claim** — `src/workers/base_worker.py` (`BaseWorker`): generic SKIP LOCKED job-queue engine
   (polling, atomic claim via `UPDATE ... WHERE ... FOR UPDATE SKIP LOCKED RETURNING`, lease
   expiry, exponential-backoff retries, a periodic reaper for stuck/expired leases, and a
   heartbeat file for container healthchecks). `ScanWorker` (`src/workers/scan_worker.py`)
   subclasses it and only implements `process_job()`, which opens a session and delegates to
   `ScanJobHandler`. Any new pipeline stage (e.g. future EXTRACT/NORMALIZE) should follow the same
   pattern: subclass `BaseWorker`, implement `process_job()`, raise `TransientProcessingError` vs
   `PermanentProcessingError` (`src/core/errors.py`) to control retry vs. immediate-fail behavior.
3. **Validate/Scan/Promote** — `src/modules/document_pipeline/scan_job_handler.py`
   `ScanJobHandler.process()`: fetches the document, re-reads bytes from quarantine, runs
   `ValidationService` (`src/modules/document_pipeline/validation.py` — 8 fail-fast checks: size,
   MIME, magic bytes, EOF trailer, heuristic threat scan, encryption, page count, SHA-256 — a
   decompression-bomb ratio check was tried and removed, see `KNOWN_DEBTS.md`), then either
   rejects (purges quarantine object, sets `REJECTED`) or
   promotes (copies to `raw/` bucket keyed by SHA-256, sets `AWAITING_CLASSIFICATION`). Dedup and
   promotion are guarded by an in-process lock (`self._promotion_lock`) plus a DB partial unique
   index (`uq_documents_active_sha256`) as the real guarantee under multi-worker concurrency.
   Handler methods are idempotent — re-running `process()` on an already-finalized document is a
   safe no-op that returns the existing state (important since jobs can be retried/reaped).

### Storage: 2-bucket + repository abstraction

- `src/storage/object_storage.py` defines `ObjectStorage` (abstract) with `LocalFileSystemStorage`
  (dev, writes under `./storage_data/`) and `MinIOStorage` (prod) implementations, selected via
  `BucketManager` per `settings.STORAGE_BACKEND`. Buckets: `quarantine/` (untrusted, purged after
  promotion or rejection) → `raw/` (validated only). `extracted/`/`normalized/` buckets exist in
  design docs for future stages but aren't wired up yet.
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

**Revision id length**: `alembic_version.version_num` defaults to `VARCHAR(32)` — keep every new
revision id ≤32 characters, or the final version-bump statement fails and rolls back the entire
migration transaction (this happened once during 0006's development).

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
