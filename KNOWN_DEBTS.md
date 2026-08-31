# Known Technical Debts

Debts carried forward intentionally. Not urgent. All will bite if forgotten.

---

## 1. Audit writes are best-effort, not transactionally guaranteed

**Status**: Shortcut — accepted for Phase 1-3, must fix before production.

`UploadService._audit()` catches exceptions and logs at ERROR level. The state change (e.g., `doc.status = PROMOTED`) commits in one transaction, the audit write commits in a separate transaction. If the audit write fails, the state change still succeeds — meaning there can be state transitions without corresponding audit rows.

**Why this matters**: The audit trail is a compliance guarantee. If a senior or auditor asks "did event X happen?", the honest answer is "probably, unless the audit write silently failed." That's not good enough.

**Fix**: Make `AuditService.log_event()` join the same DB session/transaction as the state change. Commit once at the end. `InMemory` path passes `None` and skips audit (already correct). This is a Phase 4 refactor item — when `PostgreSQLDocumentRepository` is wired to the API layer with a proper session, share that session with audit.

---

## 2. Separate-transaction pattern is a test-double workaround

**Status**: Shortcut — not a chosen design.

The reason audit writes are in a separate transaction is that `UploadService` uses `InMemoryDocumentRepository` (no DB session) in tests, but `AuditService.log_event()` requires a `Session`. We pass `db_session` as a separate parameter to `UploadService.__init__()` because the repository abstraction doesn't expose a session.

**Why this matters**: If anyone defends this as a deliberate pattern in a code review, that's wrong. The honest answer is: "We took a shortcut because of the test double, and it's on the list to fix."

**Fix**: When `PostgreSQLDocumentRepository` becomes the default in the API layer, extract the session from the repo and share it with `AuditService`. One session, one transaction, one commit.

---

## 3. No integration tests against real PostgreSQL

**Status**: Gap — SQLite is used as a stand-in for all DB tests.

All DB-related tests (`audit_log_immutability`, `check_constraint_rejects_invalid_enum_values`, audit wiring tests) use `sqlite:///:memory:`. SQLite doesn't validate real Postgres behavior:
- `FOR UPDATE SKIP LOCKED` (job queue pattern)
- `JSONB` operators
- Partial unique index behavior under concurrent transactions
- `TIMESTAMPTZ` precision

**Fix**: Add at least one test suite that runs against actual Postgres. `testcontainers-python` does this cleanly. Budget 30-60 minutes for test fixture setup. Do this before Phase 4.

---

## 4. UploadService validation/scanning must be extracted for Phase 6 workers

**Status**: Future refactor — not a bug today.

The `Job` table schema is ready for `SELECT ... FOR UPDATE SKIP LOCKED` workers (`idx_jobs_pickup` partial index, `worker_id`, `lease_expires_at`, `retry_count`). But the actual validation and scanning logic lives inside `UploadService.upload()` — a synchronous, monolithic method.

For Phase 6 (async workers), the validation/scanning needs to be extracted into a worker handler that consumes jobs independently. This is a real refactor of `UploadService`, not a slot-in.

**Fix**: When starting Phase 6, extract validation into a standalone handler (e.g., `ScanWorkerHandler`) that the worker loop invokes. Don't try to reuse `UploadService.upload()` directly from a background worker.

---

## 5. Synchronous architecture limits concurrency

**Status**: By design for Phase 1-3. Blocks production scale.

Everything is synchronous: SQLAlchemy sessions, file I/O, validation. The system cannot handle 50 concurrent uploads efficiently. `threading.Lock` in `UploadService._promotion_lock` serializes all promotions.

**Honest scalability scores**:
- As-built: 6/10
- As-designed (after Phase 6 workers): 8/10

**Fix**: Phase 6 async workers handle the heavy lifting. Phase 7 may require `asyncpg` migration for the API layer — that's a real migration (rewriting every DB call site, session management, and test), not a checkbox.
