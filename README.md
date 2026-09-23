# A-PAG AI Knowledge Base Platform

> Async ingestion foundation for a governed RAG platform, verified for 50 internal users, designed to scale to 500 without architectural changes.

---

## 🎯 What We Are Building

A unified AI system for the **Air Pollution Action Group (A-PAG)** that handles two primary types of organizational questions:

1. **Document Knowledge (RAG)**: Answering policy and project questions from unstructured PDFs using vector search.
2. **Structured Data (Text-to-SQL)**: Answering financial and operational metrics from PostgreSQL using validated natural-language-to-SQL generation.

```text
                         User Question
                               │
                         AI Chat Layer
                               │
                      Intent Query Routing
                     /                    \
                    /                      \
                   ↓                        ↓
           Knowledge Base (RAG)         PostgreSQL (Text-to-SQL)
                   │                                │
             Vector Search                   SQL Generation
             (Qdrant DB)                     (Read-only DB)
                   │                                │
           Relevant Chunks                   Query Results
                   │                                │
                   └───────────────┬────────────────┘
                                   ↓
                         Validated AI Response
```

---

## 🏗️ Core Ingestion Architecture (Asynchronous)

The ingestion pipeline executes asynchronously to protect API responsiveness, isolate CPU-heavy scanning/validation, and ensure resilience against service interruptions:

```text
Upload ──► FastAPI (POST /upload) ──► Quarantine Storage + Postgres (documents + jobs + audit)
                  │                                                          │
             202 Accepted (<500ms)                                           │
                                                                             ▼
                                                               ScanWorker Daemon
                                                     (SELECT ... FOR UPDATE SKIP LOCKED)
                                                                             │
                                                              8-Point Validation + ClamAV Scan
                                                                             │
                                                        ┌────────────────────┴────────────────────┐
                                                        ▼                                         ▼
                                                [Validation Passed]                       [Threat/Corrupt]
                                                        │                                         │
                                                Promote to Raw Bucket                     Purge Quarantine
                                                Set Status VALIDATED                      Set Status REJECTED
                                                Emit DOCUMENT_PROMOTED                    Emit DOCUMENT_REJECTED
                                                        │
                                                        ▼
                                              ExtractionWorker Daemon
                                    (pdfplumber / python-docx / python-pptx / openpyxl)
                                                        │
                                        ┌───────────────┴───────────────┐
                                        ▼                               ▼
                                [Text Extracted]                [Unreadable File]
                                        │                               │
                                Write extracted/{id}.json      Set Status EXTRACTION_FAILED
                                Set Status EXTRACTED
                                        │
                                        ▼
                                NormalizationWorker Daemon
                        (clean text, detect language, quality gate)
                                        │
                            ┌───────────┴───────────┐
                            ▼                       ▼
                    [Quality Passed]         [Quality Failed]
                            │                       │
                Write normalized/{id}.json   Set Status NORMALIZATION_FAILED
                Set Status AWAITING_CLASS    (e.g. LOW_TEXT_DENSITY — the signal
                                               a scanned document reached the system)
```

- **Quarantine-First Isolation**: Files land in isolated temporary storage before any parsing or scanning.
- **Immediate 202 Accepted**: API returns document UUID, status URL, and correlation ID in under 500ms.
- **SKIP LOCKED Worker Pool**: Background workers pull jobs without blocking, managed by 60s leases and a 30s dead-worker reaper. One worker process per pipeline stage (`WORKER_STAGE=SCAN|EXTRACT|NORMALIZE`), each its own container off the same image.
- **SHA-256 Deduplication & Partial Unique Index**: Hardware-accelerated hashing prevents duplicate storage while permitting superseded version history.
- **Append-Only Audit Trail**: Every document lifecycle event is immutably logged with correlation IDs.
- **Non-ML text extraction**: reads structure each format already states (Word styles, slide titles, worksheet grids) rather than inferring it — no OCR, no layout-inference model. See Current Limitations below.

---

## 💾 Storage Architecture

| System | Role | Contents |
|---|---|---|
| **PostgreSQL 16** | Relational Database | Document metadata (incl. full-text search index), users, background job queues, audit logs, and operational data. |
| **MinIO** | Object Storage | Document artifacts across buckets (`quarantine/`, `raw/`, `extracted/`, `normalized/`). |
| **Qdrant** | Vector Database | Document embeddings, chunk payloads, and permission metadata for semantic search. |

---

## ⚠️ Current Limitations

- **Open registration**: `POST /auth/register` has no invite gate yet — acceptable only for the internal, not-internet-exposed bootstrap phase. See `KNOWN_DEBTS.md`.
- **2-tier classification only**: `RESTRICTED` = uploader + ADMIN (owner-scoped, not department-based); no per-document ACLs. See `ARCHITECTURE.md` §6b.
- **Single-Tenant Deployment**: Multi-organization partitioning is deferred to later milestones.
- **No OCR**: Pipeline implements Stages 1–5 (quarantine → validation → promotion → text extraction → normalization). Text extraction is deliberate and non-ML — it reads structure each format already states rather than inferring it — and there is no OCR fallback, since this corpus is digitally authored, not scanned. A document with no real text layer is stopped at `NORMALIZATION_FAILED` (`LOW_TEXT_DENSITY`/`EMPTY_TEXT`) rather than silently indexed empty. See `KNOWN_DEBTS.md` #13–14.
- **No chunking/embedding/classification yet**: extraction and normalization produce clean, structured per-document JSON (`extracted/{id}.json`, `normalized/{id}.json`); nothing downstream of that (chunking, vector indexing, classification) is built yet.

### Supported upload formats

| Format | Extension | Unit count recorded |
|---|---|---|
| PDF | `.pdf` | pages |
| Word | `.docx` | — (Word text reflows; there is no page count until the document is rendered) |
| Excel | `.xlsx` | worksheets |
| PowerPoint | `.pptx` | slides |

The format is resolved from the file's **contents**, not its name or the MIME type the browser
declares — so a `.docx` that your OS reports as `application/octet-stream` still uploads, and a
spreadsheet renamed to `.docx` is stored as the spreadsheet it actually is.

Not accepted, with the reason:

- **Macro-enabled files** (`.docm`/`.xlsm`/`.pptm`, or any file containing a macro project) — re-save without macros. This is the one restriction that isn't just plumbing: a downloaded Office file does eventually get opened in Word or Excel by a person, and that executes macros.
- **Legacy or password-protected Office files** (`.doc`/`.xls`/`.ppt`, encrypted `.docx`) — re-save as the modern format, or remove the password.
- **Google Docs/Sheets/Slides** — these aren't files; they live in Drive and have no bytes to upload. Use *File → Download → Microsoft Excel (.xlsx)* (or Word/PowerPoint) and upload the result.
- **CSV** — out of scope for now; it has no container structure to validate and carries a different (formula-injection) risk profile.

---

## 🗺️ Roadmap & Phase Status

| Phase | Description | Status |
|---|---|---|
| **Phase 1** | Ingestion & Quarantine Pipeline (Validation, Structure Checks) | ✅ Completed |
| **Phase 2** | Threat Scanning & Deduplication Engine (ClamAV, SHA-256) | ✅ Completed |
| **Phase 3** | Storage Promotion, DB Migrations & Immutable Audit Log | ✅ Completed |
| **Phase C** | Asynchronous Architecture Refactor (SKIP LOCKED Workers, 202 Contract) | ✅ Completed |
| **Phase 4** | Document Text Extraction (native parsing, no OCR — see `KNOWN_DEBTS.md` #14) | ✅ Completed |
| **Phase 5** | Normalization (cleaning, language detection, quality gate) | ✅ Completed |
| **Phase 6a** | Chunking (structure-aware, citation metadata) | ✅ Completed |
| **Phase 6b** | Embedding & Vector Indexing (pgvector, self-hosted model) | 📋 Planned |
| **Phase 7** | Permission Governance, Hard Pre-Filtering & RBAC | 📋 Planned |
| **Phase 8** | Text-to-SQL Engine & Sovereign RAG Query Layer | 📋 Planned |

---

## 🚀 Quick Start Guide

### 1. Prerequisites
- **Python 3.12+**
- **Docker & Docker Compose**

### 2. Environment Configuration
```bash
cp .env.example .env
```

### 3. Start Infrastructure & Background Services
```bash
# Starts PostgreSQL, MinIO, API, and one worker container per pipeline stage
# (scan, extraction, normalization)
docker compose up -d

# Run database schema migrations
alembic upgrade head
```

### 4. Interactive Endpoints
- **API Documentation (Swagger)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Check**: [http://localhost:8000/health](http://localhost:8000/health)
- **Register**: `POST /api/v1/auth/register`
- **Login (OAuth2 password flow)**: `POST /api/v1/auth/login` — form fields `username` (email), `password`
- **Current user**: `GET /api/v1/auth/me`
- **Upload Document(s) (Async 202)**: `POST /api/v1/documents/upload` — multipart `files` (1–10), requires bearer token
- **Query Status**: `GET /api/v1/documents/{document_id}/status`
- **List Documents (paginated)**: `GET /api/v1/documents?limit=&offset=`
- **Full-Text Search**: `GET /api/v1/documents/search?q=`

All `/api/v1/documents/*` endpoints require `Authorization: Bearer <token>` from `/auth/login`.

---

## 🧪 Testing & Verification

```bash
# Run the complete test suite (52 tests: 33 unit + 19 PostgreSQL integration, ~5s)
pytest tests/ -v

# Run linter
ruff check src/ tests/ main.py
```
