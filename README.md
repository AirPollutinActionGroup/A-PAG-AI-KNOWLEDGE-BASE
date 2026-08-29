# 🌫️ A-PAG AI Knowledge Base Platform

[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg?style=flat&logo=FastAPI&logoColor=white)](https://fastapi.tiangolo.com)
[![Python](https://img.shields.io/badge/Python-3.12+-3776AB.svg?style=flat&logo=python&logoColor=white)](https://www.python.org/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16-4169E1.svg?style=flat&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-D71F00.svg?style=flat&logo=sqlalchemy&logoColor=white)](https://www.sqlalchemy.org/)
[![Tests](https://img.shields.io/badge/Tests-19%20Passing%20(100%25)-10B981.svg?style=flat&logo=pytest&logoColor=white)](TEST_REPORT.md)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

> Sovereign, secure, and resilient document intelligence pipeline built for the **Air Pollution Action Group (A-PAG)** to ingest, validate, classify, deduplicate, and catalog environmental policy directives, state action plans, and regulatory compliance documents.

---

## 📑 Table of Contents

- [Architectural Overview](#-architectural-overview)
- [Pipeline Lifecycle & Flow](#-pipeline-lifecycle--flow)
- [Project Directory Structure](#-project-directory-structure)
- [Quick Start Guide](#-quick-start-guide)
- [Interactive Testing Studio](#-interactive-testing-studio)
- [Database Schema & Migrations](#-database-schema--migrations)
- [API Reference](#-api-reference)
- [Testing & Quality Assurance](#-testing--quality-assurance)
- [Git Branching Workflow](#-git-branching-workflow)

---

## 🏛️ Architectural Overview

The platform enforces a sovereign, quarantine-first security posture designed to process large volumes of multi-format government PDFs while guaranteeing data integrity and threat isolation:

```
[ PDF Upload ] ──► 1. Quarantine Landing (apag-quarantine/{uuid}.pdf)
                         │
                         ▼
                   2. Fail-Fast 8-Point Validation Engine
                         │ (MIME, Size, Header %PDF-, Trailer %%EOF, ClamAV, Encryption, Pages, SHA-256)
                         │
         ┌───────────────┴───────────────┐
         ▼                               ▼
   [ INVALID ]                     [ VALID ]
   • Status = REJECTED             • Check SHA-256 (Dedup)
   • Purge quarantine              • Check supersedes_id (Versioning)
   • Log to audit_log              • Promote to 'apag-raw/{sha256}.pdf'
                                   • Purge quarantine
                                   • Status = AWAITING_CLASSIFICATION
                                   • Log to audit_log
```

### Core Design Principles:
1. **2-Bucket Isolation Model**:
   - `apag-quarantine`: Untrusted landing zone. No external service or reader accesses files here.
   - `apag-raw`: Promoted originals, indexed cryptographically by SHA-256 checksums (`apag-raw/{sha256}.pdf`).
2. **Fail-Fast Security Pipeline**:
   - Immediate rejection of malicious embedded scripts (`/Launch`, `/JavaScript`), password-protected PDFs, broken headers (`%PDF-`), missing trailers (`%%EOF`), and oversized files (>50MB).
3. **Cryptographic Deduplication & Versioning**:
   - Prevents duplicate compute and storage by matching raw SHA-256 hashes against existing records.
   - Supports document version updates (`version = version + 1`) and transitions historical policies to `SUPERSEDED` or `ARCHIVED`.
4. **PostgreSQL SKIP LOCKED Job Queue**:
   - Background worker queue powered by `SELECT FOR UPDATE SKIP LOCKED` on the `jobs` table with exponential retry backoff (`scheduled_at`) and dead-worker lease recovery (`lease_expires_at`).
5. **Append-Only Immutable Audit Log**:
   - Every document state transition is recorded in `audit_log` with `BIGSERIAL` sequential IDs, `TIMESTAMPTZ`, and trace `correlation_id`. Database permissions strictly revoke `UPDATE` and `DELETE`.

---

## 📂 Project Directory Structure

```
.
├── .env.example                               # Environment template with Postgres & MinIO
├── .env                                       # Local development secrets (gitignored)
├── .gitignore                                 # Production exclusion rules
├── requirements.txt                           # Production dependencies
├── pyproject.toml                             # Packaging, Pytest & Ruff configuration
├── alembic.ini                                # Database migration configuration
├── docker-compose.yml                         # Local PostgreSQL 16 & MinIO containers
├── main.py                                    # Application server entrypoint
├── TEST_REPORT.md                             # Official test execution report
├── README.md                                  # Platform documentation
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
│   │           └── 0003_revoke_audit_log_writes.py # Enforced append-only audit log
│   ├── modules/
│   │   ├── audit/
│   │   │   └── service.py                     # AuditService for immutable event logging
│   │   └── document_pipeline/
│   │       ├── models.py                      # Domain models (Document, UploadRequest, etc.)
│   │       ├── repository.py                  # PostgreSQL & InMemory document repositories
│   │       ├── validation.py                  # 8-point fail-fast validator & ClamAV scanner
│   │       └── upload_service.py              # Ingestion orchestration (Quarantine -> Validate -> Promote)
│   ├── static/
│   │   └── index.html                         # Interactive Web Testing Studio UI
│   └── storage/
│       ├── object_storage.py                  # LocalFileSystemStorage & MinIOStorage drivers
│       └── bucket_manager.py                  # 2-bucket layout manager (quarantine, raw)
└── tests/
    ├── fixtures/                              # Test sample generator
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

# Switch to active feature branch
git checkout feature/ingestion-pipeline-step-1-to-3

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

Open **`http://localhost:8000`** in any browser to access the built-in Studio:

- 📤 **Upload Hub**: Drag & drop any PDF, select 2-Tier classification (`PUBLIC` / `RESTRICTED`), and configure document versioning.
- ⚡ **One-Click Synthetic Presets**: Test standard digital PDFs, corrupted headers, truncated EOF trailers, and malicious scripts with one click.
- 🔍 **Live Pipeline Inspector**: Watch real-time visual progress across Stage 1 (Quarantine) $\rightarrow$ Stage 2 (Validation) $\rightarrow$ Stage 3 (Promotion/Rejection).
- 📑 **Documents Ledger**: Real-time auto-refreshing table showing all documents in the repository with SHA-256 hashes and status badges.

---

## 📊 Database Schema & Tables

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
PostgreSQL-backed queue table for `SKIP LOCKED` worker processes:
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

The codebase includes an exhaustive test suite covering unit, security, integration, and database constraints:

```bash
# Run all tests with verbose output
uv run pytest tests/ -v

# Run linter checks
uv run ruff check src/ tests/ main.py
```

### Test Coverage Summary:
- ✅ **Quarantine & Promotion**: Standard PDF promotion to `apag-raw/{sha256}.pdf`.
- ✅ **SHA-256 Deduplication**: Identical byte uploads marked `DUPLICATE` without duplicating storage.
- ✅ **Document Versioning**: Increments version and transitions older documents to `SUPERSEDED`.
- ✅ **Header & Trailer Integrity**: Rejects malformed headers and missing `%%EOF` markers.
- ✅ **Threat Scanner**: Intercepts weaponized `/Launch` and `/JavaScript` exploits.
- ✅ **Size & Boundary Limits**: Enforces 50MB ceiling and rejects 0-byte files.
- ✅ **Encrypted PDFs**: Rejects password-locked documents gracefully without crashes.
- ✅ **SQL Constraints**: Verifies that invalid enum strings are rejected by database `CHECK` constraints.

*(Full test report available in [`TEST_REPORT.md`](TEST_REPORT.md))*

---

## 🌿 Git Branching Workflow

```
main (Production Release)
  └── main_ci (Continuous Integration)
        └── feature/ingestion-pipeline-step-1-to-3 (Active Development)
```

- **`main`**: Stable, production-ready releases.
- **`main_ci`**: Integration branch for automated builds and testing.
- **`feature/*`**: Isolated feature branches for development.

---

## ⚖️ License & Sovereign Compliance

Developed for the **Air Pollution Action Group (A-PAG)**. All rights reserved. Configured strictly for sovereign data residency and environmental policy intelligence.
