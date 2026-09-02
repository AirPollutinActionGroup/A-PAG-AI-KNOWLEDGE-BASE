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

## 6. PostgreSQL `SKIP LOCKED` Queue (Not Kafka / RabbitMQ / Redis)

**Decision**: Job orchestration leverages PostgreSQL table-based queuing with `SELECT ... FOR UPDATE SKIP LOCKED` instead of external message brokers.

**Trade-Off Rationale**:
- For the operational scale of A-PAG (50 to 500 internal users and thousands of documents), operating an external Kafka or RabbitMQ cluster introduces substantial maintenance, synchronization, and deployment overhead.
- Postgres table-backed queues guarantee transactional enqueuing: document insertion and job creation occur within the exact same database transaction, eliminating dual-write inconsistencies.
- `SKIP LOCKED` delivers non-blocking, multi-worker concurrency with built-in leases and dead-worker reclamation out of the box.
