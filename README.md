# A-PAG AI Knowledge Base — Ingestion Platform

[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat&logo=FastAPI&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1.svg?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00.svg?style=flat&logo=sqlalchemy&logoColor=white)](https://www.sqlalchemy.org/)
[![Tests](https://img.shields.io/badge/Tests-19%20Passing-10B981.svg?style=flat&logo=pytest&logoColor=white)](TEST_REPORT.md)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

> A document intelligence platform for environmental policy and compliance documents. This repository contains the **Ingestion Layer (Stages 1–3)**: the security, storage, validation, and cataloging foundation. Extraction, normalization, and semantic retrieval are planned for subsequent phases.

---

## 📑 Table of Contents

- [System Scope & Roadmap](#-system-scope--roadmap)
- [Ingestion Architecture](#-ingestion-architecture)
- [Project Directory Structure](#-project-directory-structure)
- [Quick Start Guide](#-quick-start-guide)
- [Interactive Testing Studio](#-interactive-testing-studio)
- [Configuration Reference](#-configuration-reference)
- [Database Schema & Migrations](#-database-schema--migrations)
- [API Reference](#-api-reference)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [Current Status & Known Limitations](#-current-status--known-limitations)
- [License](#-license)

---

## 🎯 System Scope & Roadmap

The complete platform transforms multi-format government PDFs (CAQM directives, DPCC orders, NCAP action plans, CPCB guidelines) into structured, queryable knowledge:

```
┌─────────────────────────────────────────────────────────────────────────────────────────┐
│                       A-PAG AI KNOWLEDGE BASE ARCHITECTURE                              │
└─────────────────────────────────────────────────────────────────────────────────────────┘

 [ Stage 1: Ingestion & Quarantine ] ──► [ Stage 2: Fail-Fast Validation ]
                │                                         │
                ▼                                         ▼
 [ Stage 3: Dedup, Versioning & DB ] ──► [ Stage 4: Structure & Table Extraction ]
                │                                         │
                ▼                                         ▼
 [ Stage 5: Normalization & Entities ] ► [ Stage 6: Hybrid Search & Grounded RAG ]
```

### 🛣️ End-to-End Pipeline Roadmap

| Stage | Domain / Function | Scope | Status |
|:---:|---|---|:---:|
| **Stage 1** | **Quarantine Ingestion** | Untrusted multi-part upload landing, isolated temporary storage (`apag-quarantine/`), returns `202 Accepted`. | ✅ **Complete** |
| **Stage 2** | **Fail-Fast Validation** | 7-point validation suite (MIME check, 50MB limit, `%PDF-` header, `%%EOF` trailer, ClamAV exploit check, password detection, page ceiling). | ✅ **Complete** |
| **Stage 3** | **Dedup, Versioning & Storage** | SHA-256 deduplication, version chain (`supersedes_id`), promotion to `apag-raw/{sha256}.pdf`, and PostgreSQL ORM cataloging. | ✅ **Complete** |
| **Stage 4** | **Structure Extraction** | Multi-engine text parsing, layout preservation, complex table extraction (HTML/Markdown), and figure/diagram parsing. | ⏳ *Planned* |
| **Stage 5** | **Domain Normalization** | Entity tagging (CAQM, GRAP, CPCB, DPCC, PM2.5, NOx), statutory metadata enrichment, and normalized JSON schema output. | ⏳ *Planned* |
| **Stage 6** | **Hybrid Search & Grounded RAG** | Dense vector embeddings, BM25 sparse keyword search, cross-encoder reranking, and citation-backed LLM policy retrieval. | ⏳ *Planned* |

---

## 🏛️ Ingestion Architecture

```
[ Upload PDF ] ──► 1. Quarantine Landing (apag-quarantine/{uuid}.pdf)
                         │
                         ▼
                   2. Fail-Fast Validation Suite
                         │ (MIME, Size, %PDF-, %%EOF, ClamAV, Password, Pages)
                         │
         ┌───────────────┴───────────────┐
         ▼                               ▼
   [ INVALID ]                     [ VALID ]
   • Status = REJECTED             • Check SHA-256 (Deduplication)
   • Purge quarantine              • Check supersedes_id (Versioning)
   • Log to audit_log              • Promote to 'apag-raw/{sha256}.pdf'
                                   • Purge quarantine
                                   • Status = AWAITING_CLASSIFICATION
                                   • Log to audit_log
```

### Core Design Principles:
1. **2-Bucket Isolation Model**:
   - `apag-quarantine`: Untrusted landing zone. Direct reads from external services are prohibited.
   - `apag-raw`: Promoted originals, indexed cryptographically by SHA-256 checksums (`apag-raw/{sha256}.pdf`).
2. **Fail-Fast Validation**:
   - Rejection of malicious embedded scripts (`/Launch`, `/JavaScript`), password-protected PDFs, broken headers (`%PDF-`), missing trailers (`%%EOF`), and oversized files (>50MB).
3. **SHA-256 Deduplication & Versioning**:
   - Deduplicates identical file payloads at ingestion time.
   - Preserves historical policy versions via `supersedes_id` self-referential foreign keys.
4. **PostgreSQL-Backed Job Queue (Schema Ready)**:
   - `jobs` table schema supports `SELECT FOR UPDATE SKIP LOCKED` with exponential retry backoff (`scheduled_at`) and dead-worker lease recovery (`lease_expires_at`).
5. **Append-Only Audit Log**:
   - Every document state transition is logged in `audit_log` with `BIGSERIAL` event IDs, `TIMESTAMPTZ`, and trace `correlation_id`.

---

## 📂 Project Directory Structure

```
.
├── .env.example                               # Environment template with Postgres & MinIO defaults
├── .env                                       # Local development secrets (gitignored)
├── .gitignore                                 # Exclusion rules for local volumes and caches
├── requirements.txt                           # Pinned dependencies
├── pyproject.toml                             # Packaging, Pytest & Ruff configuration
├── alembic.ini                                # Database migration runner config
├── docker-compose.yml                         # Local PostgreSQL 16 & MinIO containers
├── main.py                                    # Application server entrypoint
├── TEST_REPORT.md                             # Official test execution report (19/19 passing)
├── README.md                                  # Platform technical documentation
├── src/
│   ├── api/
│   │   └── v1/
│   │       ├── ingestion.py                   # POST /upload, GET /status, GET /documents
│   │       └── router.py                      # FastAPI router aggregation & Studio UI delivery
│   ├── core/
│   │   └── config.py                          # Type-safe configuration via Pydantic BaseSettings
│   ├── db/
│   │   ├── engine.py                          # SQLAlchemy sync engine + connection pooling
│   │   ├── enums.py                           # String-backed enums (DocumentStatus, JobStage, etc.)
│   │   ├── models.py                          # Document, Job, AuditLog SQLAlchemy 2.0 ORM models
│   │   └── migrations/                        # Alembic versioned migration scripts
│   │       ├── env.py                         # Migration runtime environment
│   │       └── versions/
│   │           ├── 0001_initial_schema.py     # Base DDL tables & indexes
│   │           ├── 0002_add_check_constraints.py # Database-level CHECK constraints
│   │           └── 0003_revoke_audit_log_writes.py # Audit log write revocation
│   ├── modules/
│   │   ├── audit/
│   │   │   └── service.py                     # AuditService for immutable event logging
│   │   └── document_pipeline/
│   │       ├── models.py                      # Domain models (Document, UploadRequest, etc.)
│   │       ├── repository.py                  # PostgreSQL & InMemory document repositories
│   │       ├── validation.py                  # Fail-fast validator & ClamAV scanner
│   │       └── upload_service.py              # Ingestion orchestration (Quarantine -> Validate -> Promote)
│   ├── static/
│   │   └── index.html                         # Interactive Web Testing Studio UI
│   └── storage/
│       ├── object_storage.py                  # LocalFileSystemStorage & MinIOStorage drivers
│       └── bucket_manager.py                  # 2-bucket layout manager (quarantine, raw)
└── tests/
    ├── fixtures/                              # Synthetic test fixture PDF generator
    └── test_ingestion_pipeline.py             # 19 master test scenarios (100% PASS)
```

---

## 🚀 Quick Start Guide

### 1. Prerequisites
- **Python 3.12+**
- **uv** package manager (`pip install uv` or `curl -LsSf https://astral.sh/uv/install.ps1 | iex`)
- **Docker & Docker Compose** (for PostgreSQL and MinIO)

### 2. Clone & Environment Setup
```bash
# Clone the repository
git clone https://github.com/AirPollutinActionGroup/A-PAG-AI-KNOWLEDGE-BASE.git
cd A-PAG-AI-KNOWLEDGE-BASE

# Create virtual environment & install dependencies
uv venv
.venv\Scripts\activate      # On Windows (or 'source .venv/bin/activate' on Linux/macOS)
uv pip install -r requirements.txt
```

### 3. Start Local Infrastructure
```bash
# Launch PostgreSQL 16 and MinIO
docker compose up -d
```

### 4. Run Database Migrations
```bash
# Apply all migrations to the latest version
uv run alembic upgrade head
```

### 5. Launch API Server & Testing Studio
```bash
uv run python main.py
```
- **Web Testing Studio**: [http://localhost:8000](http://localhost:8000)
- **Interactive Swagger Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Probe**: [http://localhost:8000/health](http://localhost:8000/health)

---

## 🖥️ Interactive Testing Studio

Open **`http://localhost:8000`** in any browser to access the built-in testing interface:

- 📤 **Upload Hub**: Drag & drop any PDF, select 2-Tier classification (`PUBLIC` / `RESTRICTED`), and test document versioning.
- ⚡ **One-Click Synthetic Presets**: Test standard digital PDFs, corrupted headers, truncated EOF trailers, and malicious scripts with one click.
- 🔍 **Live Pipeline Inspector**: Watch real-time visual progress across Stage 1 (Quarantine) $\rightarrow$ Stage 2 (Validation) $\rightarrow$ Stage 3 (Promotion/Rejection).
- 📑 **Documents Ledger**: Real-time auto-refreshing table showing all documents in the repository with SHA-256 hashes and status badges.

---

## ⚙️ Configuration Reference

All settings are managed via type-safe Pydantic Settings in `src/core/config.py` and read from `.env`:

| Variable | Type | Default | Description |
|---|:---:|:---:|---|
| `APP_ENV` | `str` | `development` | Environment name (`development`, `staging`, `production`). |
| `LOG_LEVEL` | `str` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, `ERROR`). |
| `DATABASE_URL` | `str` | `postgresql://apag_user:...@localhost:5432/apag_kb` | PostgreSQL connection string. |
| `STORAGE_BACKEND` | `str` | `local` | Storage driver (`local`, `minio`, `azure`). |
| `LOCAL_STORAGE_BASE_DIR` | `str` | `./storage_data` | Directory for local file storage driver. |
| `MINIO_ENDPOINT` | `str` | `localhost:9000` | MinIO host and port. |
| `MINIO_ACCESS_KEY` | `str` | `minioadmin` | MinIO access key. |
| `MINIO_SECRET_KEY` | `str` | `minioadmin` | MinIO secret key. |
| `QUARANTINE_BUCKET_NAME` | `str` | `apag-quarantine` | Bucket for unverified incoming uploads. |
| `RAW_BUCKET_NAME` | `str` | `apag-raw` | Bucket for validated, promoted documents. |
| `MAX_UPLOAD_SIZE_MB` | `int` | `50` | Maximum allowed document upload size. |

---

## 📊 Database Schema & Migrations

### 1. `documents` (Document Ledger)
Tracks policy document metadata, cryptographic identity, and lifecycle state:
- `document_id`: Native `UUID` Primary Key.
- `filename`: Original document title.
- `file_size`: File size in bytes.
- `sha256`: 64-character hash (Indexed for deduplication).
- `status`: Lifecycle state (`UPLOADED`, `QUARANTINED`, `AWAITING_CLASSIFICATION`, `REJECTED`, `DUPLICATE`, `LIVE`, `SUPERSEDED`, `ARCHIVED`).
- `classification`: Access tier (`PUBLIC`, `RESTRICTED`, `NULL`).
- `version`: Monotonically increasing version integer.
- `supersedes_id`: Self-referential Foreign Key (`ON DELETE SET NULL`) linking to older document versions.
- `scan_flags`: `JSONB` structural validation metadata.

### 2. `jobs` (Asynchronous Queue)
PostgreSQL-backed queue table for worker processes:
- `job_id`: Native `UUID` Primary Key.
- `document_id`: Foreign Key linking to `documents.document_id` (`ON DELETE CASCADE`).
- `stage`: Pipeline stage (`SCAN`, `EXTRACT`, `NORMALIZE`).
- `status`: Execution state (`PENDING`, `RUNNING`, `COMPLETED`, `FAILED`).
- `priority`: Integer priority order.
- `scheduled_at`: `TIMESTAMPTZ` pickup timestamp supporting exponential retry backoff.
- `lease_expires_at`: `TIMESTAMPTZ` timestamp for automated crashed-worker detection and recovery.
- `idx_jobs_pickup`: Partial index on `(stage, status, scheduled_at) WHERE status = 'PENDING'`.

### 3. `audit_log` (Immutable Audit Trail)
- `event_id`: `BIGSERIAL` auto-incrementing primary key.
- `document_id`: Native `UUID`.
- `event_type`: Lifecycle event (`DOCUMENT_UPLOADED`, `DOCUMENT_QUARANTINED`, `VALIDATION_PASSED`, `VALIDATION_FAILED`, `DOCUMENT_PROMOTED`, `DOCUMENT_REJECTED`, etc.).
- `event_time`: UTC `TIMESTAMPTZ`.
- `details`: `JSONB` context payload.
- `correlation_id`: Trace UUID for cross-service request tracking.

---

## 📡 API Reference

| Method | Endpoint | Status | Description |
|---|---|:---:|---|
| `GET` | `/` | `200 OK` | Serves the interactive Ingestion Testing Studio web UI. |
| `GET` | `/health` | `200 OK` | Liveness and readiness healthcheck probe. |
| `POST` | `/api/v1/documents/upload` | `202 Accepted` | Ingests PDF into quarantine, executes validation, and returns document ID. |
| `GET` | `/api/v1/documents/{id}/status` | `200 OK` | Queries validation status, storage path, version, and rejection reasons. |
| `GET` | `/api/v1/documents` | `200 OK` | Lists all ingested documents recorded in the repository ledger. |

---

## 🧪 Testing & Quality Assurance

```bash
# Run all tests with verbose output
uv run pytest tests/ -v

# Run linter checks
uv run ruff check src/ tests/ main.py
```

### Verified Test Domains (19/19 Passing):
- ✅ **Quarantine & Promotion**: Clean PDF upload flow and promotion to `apag-raw/{sha256}.pdf`.
- ✅ **SHA-256 Deduplication**: Identical byte uploads marked `DUPLICATE` without duplicating storage.
- ✅ **Document Versioning**: Increments version and transitions older documents to `SUPERSEDED`.
- ✅ **Header & Trailer Integrity**: Rejects malformed headers (`%PDF-`) and missing `%%EOF` markers.
- ✅ **Threat Scanner**: Intercepts weaponized `/Launch` and `/JavaScript` exploits.
- ✅ **Size & Boundary Limits**: Enforces 50MB ceiling and rejects 0-byte files.
- ✅ **Encrypted PDFs**: Rejects password-locked documents gracefully without crashes.
- ✅ **SQL Constraints**: Verifies that invalid enum strings are rejected by database `CHECK` constraints.

*(Full test execution report available in [`TEST_REPORT.md`](TEST_REPORT.md))*

---

## ⚠️ Current Status & Known Limitations

1. **Synchronous Execution**: The upload endpoint currently runs validation synchronously in the HTTP request loop. Asynchronous execution via dedicated `SKIP LOCKED` worker processes is scheduled for Phase C.
2. **Worker Consumer**: The `jobs` table schema and partial indexes are fully configured, but the standalone consumer worker daemon (`worker_main.py`) is pending implementation.
3. **Storage Streaming Protocol**: Storage client streaming refactor (`BinaryIO` and `ObjectRef`) is scheduled for Phase B.
4. **Test Suite Scope**: Unit tests run against both in-memory and SQLite/Postgres fixtures; dedicated live-PostgreSQL integration test separation is scheduled for Phase D.

---

## 📄 License

Internal proprietary software developed for the Air Pollution Action Group (A-PAG). All rights reserved.
