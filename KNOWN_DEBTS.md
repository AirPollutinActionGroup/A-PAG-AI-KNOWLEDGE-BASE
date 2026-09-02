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

## 2. Active Technical Debts

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
  - The two threats that DO apply to this system's actual attack surface — parser crashes from malformed structure, and resource exhaustion from oversized/bomb files — are covered by structural validation (checks 1–7) and decompression bomb detection (check 9).
- **Trigger to revisit and add real ClamAV (`clamd` daemon)**:
  - External (non-employee) users gain upload access
  - Documents get distributed/downloaded in ways this system doesn't control (e.g., users can download and open PDFs in Adobe Reader with macros/JS enabled from within the org)
  - A specific compliance/audit requirement mandates named malware scanning software
  - A-PAG's platform expands to CEGIS/Prosperiti or other orgs with different risk tolerance
- **Estimated effort when triggered**: 4–6 hours (`clamd` Docker service, `pyclamd` client, replace `ClamAVScanner` call site, EICAR test).

