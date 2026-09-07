# Known Technical Debts

Technical debts and trade-offs tracked deliberately. Each debt is annotated with its rationale, impact, and the specific trigger for when it must be resolved.

---

## 1. Closed Debts (Resolved in Phase C - Async Foundation)

### ✅ Sync architecture limits concurrency (Closed)
- **Resolution**: Refactored ingestion pipeline to asynchronous background execution in Phase C.
- `POST /api/v1/documents/upload` accepts uploads in `<500ms` returning `202 Accepted`.
- Background worker consumes jobs via `SELECT ... FOR UPDATE SKIP LOCKED` with automatic lease management and dead-worker reaper.

### ✅ UploadService monolithic validation/scanning (Closed)
- **Resolution**: Separated into fast-path `UploadService.receive()` (quarantine landing + DB insertion + job enqueue) and standalone `ScanJobHandler.process()` (validation, ClamAV structural threat scanning, deduplication, promotion, and audit logging).

### ✅ No integration tests against real PostgreSQL (Closed)
- **Resolution**: Added 19 PostgreSQL integration tests using `testcontainers-python` verifying `FOR UPDATE SKIP LOCKED` concurrency, partial unique index deduplication, check constraints, lease reclamation, and transaction isolation.

---

### ✅ No user identity, auth, or ownership (Closed)
- **Resolution**: Added a `users` table (migration 0006), JWT-based auth
  (`src/modules/auth/`), and wired `documents.uploader_user_id` + `audit_log.user_id` end-to-end.
  All endpoints now require a bearer token via a single `get_current_user` dependency seam.
  (Migration 0006 also added a `departments` table; it was removed in migration 0007 — see the
  closed debt below.)

### ✅ Concurrent duplicate uploads crash instead of resolving to DUPLICATE (Closed)
- **Resolution**: `ScanJobHandler.process()` now catches the `IntegrityError` from
  `uq_documents_active_sha256` when two workers race to promote the same checksum, and routes
  the loser to `DUPLICATE` instead of letting the job fail and retry 3x.

### ✅ Storage I/O errors misclassified as permanent failures (Closed)
- **Resolution**: `ScanWorker.process_job` now raises `TransientProcessingError` (not
  `PermanentProcessingError`) on `STORAGE_ERROR` — a MinIO blip is retried, matching the
  quarantine-file-preserved-for-retry behavior the handler already implemented.

### ✅ Heuristic scanner false-positived on benign PDFs (Closed)
- **Resolution**: Removed `/OpenAction` (common, benign PDF directive) and bare `/JS` (collides
  with unrelated binary stream bytes) from `ClamAVScanner.MALICIOUS_SIGNATURES`. Removed the
  naive `/Encrypt` byte-scan pre-check in favor of pypdf's authoritative `reader.is_encrypted`.

### ✅ Integration tests silently deleted the dev database's documents (Closed)
- **Resolution**: `testcontainers[postgres]` was listed in `requirements.txt` but missing from
  `pyproject.toml` — the file `uv sync` actually installs from. So the package was never
  installed, and `tests/integration/conftest.py::_get_postgres_url()` caught the resulting
  `ImportError` and silently fell back to `settings.DATABASE_URL` — the real development
  database. Several integration tests (`test_worker_execution.py`,
  `test_worker_skip_locked.py`) do `session.query(DocumentORM).delete()` /
  `session.query(JobORM).delete()` to get a clean queue for each test, which is harmless
  against a disposable container but destroys real data against a dev DB. Every `pytest tests/`
  run was doing this. Fixed by adding `testcontainers[postgres]` to `pyproject.toml` and making
  the fallback refuse to run by default: it now `pytest.skip()`s with instructions instead of
  silently using `DATABASE_URL`, unless `APAG_ALLOW_DESTRUCTIVE_DB_TESTS=1` is explicitly set.
  `audit_log` rows survived throughout (DB-level immutability trigger, migration 0003) — this is
  why the audit history looked intact even as documents kept disappearing.

### ✅ Three-role model (ADMIN/CONTRIBUTOR/VIEWER) implied permissions that didn't exist (Closed)
- **Resolution**: Every permission check in the codebase only ever tested `role == ADMIN` —
  CONTRIBUTOR and VIEWER were never actually distinguished anywhere (both could upload,
  search, download, and delete their own documents identically). Migration
  `0009_collapse_user_roles` collapsed `UserRole` to `ADMIN` / `USER`, converting existing
  CONTRIBUTOR/VIEWER rows to `USER` and updating the `chk_users_role` constraint. The system
  is now honestly a 2-tier model matching what `_can_view()` and the delete endpoint actually
  enforce: ADMIN (sees/deletes every document) vs. USER (manages only their own).
- **Trigger to revisit**: if a real need appears for a role that can upload but not delete, or
  view but not upload, reintroduce a role split *and* wire `require_role()` (already defined
  in `src/modules/auth/dependencies.py`, currently unused) into the relevant endpoints —
  don't add a role value without an enforcement point using it.

### ✅ Departments table existed but was never populated, making RESTRICTED admin-only (Closed)
- **Resolution**: Migration 0006 added `departments` + `users.department_id` +
  `documents.department_id`, and `_can_view()` gated RESTRICTED visibility on department match.
  In practice no departments were ever seeded and no user ever had a `department_id` set, so
  every RESTRICTED document was visible to ADMINs only — including the uploader who set that
  classification. Migration `0007_owner_scoped_access` dropped `departments`,
  `users.department_id`, and `documents.department_id`, and replaced the visibility check with
  `doc.owner_id == current_user.user_id or current_user.role == ADMIN`. See `ARCHITECTURE.md`
  §6b.

### ✅ `documents.doc_type` accepted but never used (Closed)
- **Resolution**: Content category (POLICY/REPORT/DATASET/LEGAL/OTHER) can't be derived from a
  file — it's a document-understanding task, and the pipeline already reserves
  `AWAITING_CLASSIFICATION` for the future stage that will determine it properly. Nothing
  downstream ever read the field. Dropped in migration `0007_owner_scoped_access`; will be
  re-added once real (automatic) classification exists in Phase 4+.

### ✅ BucketManager silently ignored STORAGE_BACKEND=minio (Closed)
- **Resolution**: `BucketManager.__init__` previously defaulted `use_local=True` regardless of
  `settings.STORAGE_BACKEND`, and every default-constructed call site (`UploadService`,
  `ScanJobHandler`, `ScanWorker`, plus the API's shared `_buckets` singleton) never overrode it.
  In the dockerized deployment this meant uploads were silently written to each container's
  ephemeral local filesystem instead of MinIO — invisible in the MinIO console and lost on
  container restart (no volume mounted for `storage_data/`). Fixed by resolving all defaults
  (`use_local`, endpoint, credentials) from `settings` inside `BucketManager` itself. Verified
  live: worker log now reads `Storage backend: MinIO (minio:9000)` and uploaded files appear in
  `mc ls local/apag-raw`.

---

## 2. Active Technical Debts

### 0. Open self-registration (`POST /auth/register` has no gate)
- **Status**: Deliberate for the 50-person internal-org bootstrap phase.
- **Context**: Anyone who can reach the API can create an account with any role, including
  ADMIN. Acceptable only because the API is not internet-exposed yet and the org is small/known.
- **Trigger to address**: Before any external-network exposure, or before onboarding beyond the
  initial trusted cohort — gate behind an admin invite flow or SSO instead.

### 0b. Classification is a 2-tier model (PUBLIC / owner-scoped RESTRICTED), not per-document ACLs
- **Status**: Deliberate scope limit for 50-person scale.
- **Context**: RESTRICTED visibility = uploader + ADMIN role. No department tier, no per-document
  or per-user sharing list exists.
- **Trigger to address**: When a real team/department-based sharing need appears (with real
  departments actually populated and users actually assigned to them — the prior attempt at this
  shipped without either), add a `document_acl` join table or a department tier rather than
  expanding the enum — don't overfit the 2-tier model to a one-off request.

### 1. Audit writes are best-effort, not transactionally guaranteed
- **Status**: Accepted trade-off.
- **Context**: State changes and audit events commit in separate transaction boundaries. If an audit write encounters an unhandled exception, the document state change remains committed while an ERROR log is written.
- **Trigger to address**: When unifying database session orchestration across Phase 4 extraction pipelines.

### 2. Separate-transaction pattern for repository test-doubles
- **Status**: Maintained for in-memory unit tests.
- **Context**: `InMemoryDocumentRepository` does not bind a SQLAlchemy session, necessitating optional `db_session` injection for `AuditService`.
- **Trigger to address**: When all pipeline integration layers standardize exclusively on session-bound repositories.

### 3. Idempotency is application-layer, not DB-enforced
- **Status**: Application-guarded.
- **Context**: `ScanJobHandler` and `ScanWorker` query document status (`doc.status != "QUARANTINED"`) and existing audit records before executing promotions. A race between two workers on duplicate jobs is handled cleanly in application logic, but there is no database-level unique constraint on `(document_id, event_type)` in `audit_log`.
- **Trigger to address**: Before introducing multi-stage pipeline workflows (e.g. OCR/Extraction/Chunking) where pipeline stages can be dynamically retried.

### 4. Retry failure classification is coarse
- **Status**: Two-tier (`TransientProcessingError` vs `PermanentProcessingError`).
- **Context**: Network timeouts and database locks trigger exponential backoff retries, while corrupted PDF syntax and malware trigger immediate `FAILED`/`REJECTED` status. Edge cases (e.g., malformed scanner daemon responses) default to transient retry.
- **Trigger to address**: When production monitoring highlights specific scanner or storage edge cases requiring custom retry policies.

### 5. Single-worker container healthcheck model
- **Status**: Heartbeat file (`/tmp/worker_alive`).
- **Context**: Docker container healthcheck inspects file modification time (`stat -c %Y /tmp/worker_alive < 30s`). This model assumes one worker daemon per container.
- **Trigger to address**: When scaling to multiple worker subprocesses within a single container.

### 6. Observability and queue metrics
- **Status**: Structured application logging only.
- **Context**: Queue depth, job duration, lease renewals, and dead-letter counts are logged at INFO/DEBUG levels, but no Prometheus metrics endpoint is exposed yet.
- **Trigger to address**: Prior to user-facing production release.

### 7. Studio UI expects synchronous upload response
- **Status**: Fast-follow UI task.
- **Context**: The testing Studio UI at `/` was written for the synchronous pipeline and expects terminal `AWAITING_CLASSIFICATION` immediately from `POST /upload`. It needs to be updated to poll `GET /api/v1/documents/{id}/status` every 500ms until terminal state.
- **Trigger to address**: Before internal user onboarding.

### 8. Malware Scanning: Heuristic Only, Real ClamAV Deliberately Deferred
- **Status**: Deliberate architectural deferral.
- **Decision**: After evaluating the actual threat model, we determined real ClamAV-style signature scanning is not justified for Phase 1.
- **Reasoning**:
  - Uploads come from trusted internal employees on company-managed devices, sourced from Drive/email that already passed through upstream malware scanning (Google/email provider).
  - The pipeline never executes or renders PDF content (no PDF viewer, no macro execution, no embedded script execution) — the primary threat ClamAV-style scanning defends against (a viewer executing malicious embedded content) does not apply to how this system processes files.
  - The heuristic signature scanner (`ClamAVScanner` interface) is kept as a low-cost anomaly flag (detects `/JavaScript`, `/Launch`, `/OpenAction` patterns) — not a claim of real malware protection.
  - The two threats that DO apply to this system's actual attack surface — parser crashes from malformed structure, and resource exhaustion from oversized files — are covered by structural validation (checks 1–7) and the 100MB file-size ceiling (check 2). A dedicated decompression-bomb ratio check (formerly check 9) was tried and removed — see debt #9 below.
- **Trigger to revisit and add real ClamAV (`clamd` daemon)**:
  - External (non-employee) users gain upload access
  - Documents get distributed/downloaded in ways this system doesn't control (e.g., users can download and open PDFs in Adobe Reader with macros/JS enabled from within the org)
  - A specific compliance/audit requirement mandates named malware scanning software
  - A-PAG's platform expands to CEGIS/Prosperiti or other orgs with different risk tolerance
- **Estimated effort when triggered**: 4–6 hours (`clamd` Docker service, `pyclamd` client, replace `ClamAVScanner` call site, EICAR test).

### 9. No decompression-bomb protection beyond the 100MB file-size ceiling
- **Status**: Deliberately removed (was check #9 in `ValidationService`), not deferred.
- **Context**: The original check rejected a PDF if any internal stream's decompressed size
  exceeded `MAX_DECOMPRESSION_RATIO` (200x) its compressed size. In practice this produced
  false positives on legitimate documents — ordinary highly-compressible content (a
  solid-fill image, a mostly-blank scanned page, an embedded font's glyph table) routinely
  exceeds 200x compression while expanding to only a few hundred KB to a few MB, which is not
  a resource-exhaustion attack by any reasonable definition. Two mitigation attempts were
  tried and rejected in the same debugging session: (1) requiring a minimum absolute
  decompressed size (5MB) before the ratio could reject — still flagged real files; (2)
  keeping the ratio at 200x with no floor — the original, most false-positive-prone
  behavior. The check was removed entirely rather than continuing to tune it.
- **What still bounds resource use**: the 100MB file-size ceiling (check #2) — the only way a
  single upload can grow beyond that is by expanding after decompression, which is exactly
  the case this debt leaves unguarded.
- **Trigger to revisit**: a real incident (worker OOM/crash from a decompressed stream) — not
  a hypothetical. If revisited, don't reintroduce a compression-ratio comparison; instead cap
  the *absolute* decompressed size of any single stream against a generous, size-independent
  ceiling (e.g. reject only past 1–2GB decompressed), since ratio was the actual source of
  every false positive, not absolute size.

### 10. `db_session`'s rollback doesn't protect against tests that call `.commit()` directly
- **Status**: Worked around per-file, not fixed.
- **Context**: `tests/integration/conftest.py`'s `db_session` fixture wraps each test in a
  transaction and rolls it back at teardown — but that only undoes changes if nothing inside
  the test ever calls `.commit()`. Several tests (`test_worker_execution.py`,
  `test_worker_skip_locked.py`) exercise code paths that commit directly (`ScanJobHandler`,
  raw `session.commit()` calls setting up fixtures), which ends the outer transaction early —
  the rollback at teardown then has nothing left to undo. Both files compensate with their own
  `autouse=True` fixture that manually `DELETE`s the specific tables they touch before each
  test, which fully solves it for those two files but isn't a general guarantee for any new
  integration test that commits.
- **Why not fixed properly**: the real fix is nested `SAVEPOINT`s (what Django's `TestCase`
  does) so an inner `.commit()` releases a savepoint instead of ending the outer transaction.
  That's a real rewire of the session factory used by every repository/service under test, for
  a problem that's already fully contained by the two existing manual-cleanup fixtures.
- **Trigger to address**: if a third integration test file needs to commit and the
  copy-the-cleanup-fixture pattern starts feeling repetitive/error-prone, switch to the
  savepoint approach once, for all files, rather than adding a fourth bespoke `DELETE` fixture.
