# System Architecture & Trade-Offs

This document captures the key architectural decisions, rationale, and explicit trade-offs chosen for the A-PAG AI Knowledge Base ingestion platform.

---

## 1. PostgreSQL as the Single Source of Truth

**Decision**: All metadata, document lifecycles, job queues, and immutable audit logs are stored in PostgreSQL 16.

**Trade-Off Rationale**:
- Document governance requires strict relational integrity (foreign keys, cascading soft/hard deletes, version lineage) and ACID transactions.
- Storing document metadata in document stores (e.g. MongoDB) or search indices (e.g. Elasticsearch) risks consistency drift during multi-stage ingestion failures.
- Postgres provides rich constraint validation (CHECK constraints, partial indexes, and JSONB schemas) in a single operational dependency.

---

## 2. Background Workers & Synchronous SQLAlchemy (Not `asyncpg`)

**Decision**: Background workers run as dedicated Python worker processes using synchronous SQLAlchemy sessions and `SELECT ... FOR UPDATE SKIP LOCKED`.

**Trade-Off Rationale**:
- Document validation, PDF parsing, OCR, and ClamAV scanning are CPU and disk I/O bound tasks, where Python `asyncio` provides no throughput advantage.
- Dedicated worker processes isolate CPU-intensive workloads from the FastAPI HTTP gateway, preventing event loop starvation.
- Synchronous SQLAlchemy offers deterministic session boundaries, robust transaction rollback semantics, and simple debugging compared to async ORM session lifecycles.

---

## 3. Two-Tier Storage Architecture (`quarantine/` and `raw/`)

**Decision**: Untrusted uploads are landed into an isolated `quarantine/` bucket before validation and only promoted to `raw/` upon passing all security and structural gates.

**Trade-Off Rationale**:
- Placing unverified files directly into `raw/` exposes downstream extraction pipelines and vector indexers to malicious payloads or malformed structures.
- Strict physical separation ensures that downstream processors only ever read validated content.
- The `quarantine/` object is purged immediately upon promotion or rejection, guaranteeing no residual data leak or lingering toxic payload.

---

## 4. SHA-256 Deduplication with Partial Unique Index

**Decision**: Deduplication is enforced via a partial unique index `uq_documents_active_sha256` on `documents(sha256) WHERE status NOT IN ('SUPERSEDED', 'ARCHIVED', 'REJECTED')`.

**Trade-Off Rationale**:
- Application-level deduplication checks (`SELECT ... WHERE sha256 = ...`) suffer from race conditions under concurrent uploads of the same file.
- The partial unique index enforces zero-duplicate guarantees at the storage engine layer while natively permitting re-ingestion of historical versions if an older document is superseded or archived.

---

## 5. Append-Only Audit Trail

**Decision**: Every document transition (`QUARANTINED`, `VALIDATION_PASSED`, `PROMOTED`, `REJECTED`, `SUPERSEDED`) emits an immutable record to `audit_log`.

**Trade-Off Rationale**:
- Regulatory compliance and organizational governance require provable event ordering and non-repudiation.
- Updates and deletions on `audit_log` rows are forbidden at the database level.
- Audit records capture correlation IDs, timestamps, user identities, and event payloads to provide complete observability into ingestion lifecycles.

---

## 6a. JWT Auth Behind a Single `get_current_user` Seam (Not SSO, Yet)

**Decision**: Identity is resolved by one FastAPI dependency (`src/modules/auth/dependencies.py::get_current_user`), backed by JWT bearer tokens issued from `POST /auth/login` against `users` rows hashed with `bcrypt` directly (not `passlib` — see below).

**Trade-Off Rationale**:
- At 50 internal users with no existing SSO integration, standing up Google Workspace/Azure AD OAuth is disproportionate setup cost for the current phase.
- Every endpoint depends on `get_current_user` and nothing else — when the parent company's larger rollout requires real SSO, only that one function's internals change; no endpoint signatures move.
- `passlib`'s `CryptContext` (the usual choice for this) is unmaintained since 2020 and crashes against `bcrypt>=4.1`'s changed API (`ValueError: password cannot be longer than 72 bytes` raised from its internal backend-detection routine, unrelated to the actual password). Hashing directly via the `bcrypt` package avoids that entire compatibility surface.
- Registration (`POST /auth/register`) is deliberately open (no invite gate) for the internal bootstrap phase — see `KNOWN_DEBTS.md`.

## 6b. Two-Tier Classification: PUBLIC / Owner-Scoped RESTRICTED (Not Departments, Not Per-Document ACLs)

**Decision**: `RESTRICTED` documents are visible to their uploader (`doc.owner_id == current_user.user_id`) plus any `ADMIN`; there is no department tier and no per-document/per-user sharing list. `departments`, `users.department_id`, `documents.department_id`, and `documents.doc_type` were removed in migration `0007_owner_scoped_access` — see below for why.

**Trade-Off Rationale**:
- A full ACL model (arbitrary user/group grants per document) is real complexity — extra joins on every read path, UI for managing grants, and a much larger permission-bug surface — that a 50-person org does not need yet.
- The original design used a department tier (`doc.department_id == current_user.department_id or current_user.role == ADMIN`), but no departments were ever seeded and no user ever had a `department_id` set — which meant every RESTRICTED document was visible to ADMINs only, including the uploader who set that classification. That's a real bug in practice, not just unused scope.
- The owner-scoped model is the same one-boolean-check shape (`doc.owner_id == current_user.user_id or current_user.role == ADMIN`), applied uniformly in `_can_view()` across list/search/status endpoints — but it maps onto something every user already has (their own account) rather than an org structure that was never populated. It reads the same way Google Drive's "private to me" vs. "anyone in the org" does.
- If department- or team-level sharing becomes a real requirement later, it's a small, additive change to `_can_view()` — not a rewrite — once departments are actually populated and assigned.
- This is the model that must be re-verified inside the Qdrant retrieval path in Phase 5 (permission fields duplicated into vector payloads, filtered *during* the vector search, not after) — see `KNOWN_DEBTS.md` for the reindex-on-reclassification implication.

**Why `doc_type` was dropped, not fixed**: it stored a content category (POLICY/REPORT/DATASET/LEGAL/OTHER) that cannot be derived from the file itself — that's a document-understanding task, not a validation check. The pipeline already reserves the `AWAITING_CLASSIFICATION` status precisely for a future stage that can determine this properly (Phase 4+); a manual dropdown asked users to do by hand what the system was always meant to do automatically, and nothing downstream ever read the field. Re-added once real classification exists, not before.

## 6c. Storage Backend Resolved Centrally in `BucketManager`, Not at Each Call Site

**Decision**: `BucketManager.__init__` resolves `use_local`/endpoint/credentials from `settings` itself when not explicitly overridden, rather than requiring every caller to pass them.

**Trade-Off Rationale**:
- The prior per-call-site pattern silently broke MinIO in the actual docker-compose deployment: `BucketManager()` (used by default in `UploadService`, `ScanJobHandler`, and `ScanWorker`) and the API's shared `_buckets` singleton never passed `use_local=False`, so `use_local` defaulted `True` regardless of `STORAGE_BACKEND=minio` — uploads were written to each container's ephemeral local filesystem, invisible in MinIO and lost on restart.
- Centralizing the settings resolution means a bare `BucketManager()` is always correct for the environment it runs in, and there is exactly one place to change if storage config resolution needs to change again.

## 6. PostgreSQL `SKIP LOCKED` Queue (Not Kafka / RabbitMQ / Redis)

**Decision**: Job orchestration leverages PostgreSQL table-based queuing with `SELECT ... FOR UPDATE SKIP LOCKED` instead of external message brokers.

**Trade-Off Rationale**:
- For the operational scale of A-PAG (50 to 500 internal users and thousands of documents), operating an external Kafka or RabbitMQ cluster introduces substantial maintenance, synchronization, and deployment overhead.
- Postgres table-backed queues guarantee transactional enqueuing: document insertion and job creation occur within the exact same database transaction, eliminating dual-write inconsistencies.
- `SKIP LOCKED` delivers non-blocking, multi-worker concurrency with built-in leases and dead-worker reclamation out of the box.
