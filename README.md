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
                                                Set Status AWAITING_CLASS                 Set Status REJECTED
                                                Emit DOCUMENT_PROMOTED                    Emit DOCUMENT_REJECTED
```

- **Quarantine-First Isolation**: Files land in isolated temporary storage before any parsing or scanning.
- **Immediate 202 Accepted**: API returns document UUID, status URL, and correlation ID in under 500ms.
- **SKIP LOCKED Worker Pool**: Background workers pull jobs without blocking, managed by 60s leases and a 30s dead-worker reaper.
- **SHA-256 Deduplication & Partial Unique Index**: Hardware-accelerated hashing prevents duplicate storage while permitting superseded version history.
- **Append-Only Audit Trail**: Every document lifecycle event is immutably logged with correlation IDs.

---

## 💾 Storage Architecture

| System | Role | Contents |
|---|---|---|
| **PostgreSQL 16** | Relational Database | Document metadata, background job queues, audit logs, and operational data. |
| **MinIO** | Object Storage | PDF artifacts across buckets (`quarantine/`, `raw/`, `extracted/`, `normalized/`). |
| **Qdrant** | Vector Database | Document embeddings, chunk payloads, and permission metadata for semantic search. |

---

## ⚠️ Current Limitations

- **Testing Studio UI**: The interactive web UI currently expects a synchronous response from `/upload`; polling integration against `GET /api/v1/documents/{id}/status` is queued as a fast-follow.
- **Authentication**: Role-based access control (RBAC) and user authentication are planned for Phase 6.
- **Single-Tenant Deployment**: Multi-organization partitioning is deferred to later milestones.
- **Text Extraction & OCR**: Pipeline currently implements Stages 1–3 (quarantine, validation, scanning, promotion); Stage 4 (OCR / extraction) is the next phase.

---

## 🗺️ Roadmap & Phase Status

| Phase | Description | Status |
|---|---|---|
| **Phase 1** | Ingestion & Quarantine Pipeline (Validation, Structure Checks) | ✅ Completed |
| **Phase 2** | Threat Scanning & Deduplication Engine (ClamAV, SHA-256) | ✅ Completed |
| **Phase 3** | Storage Promotion, DB Migrations & Immutable Audit Log | ✅ Completed |
| **Phase C** | Asynchronous Architecture Refactor (SKIP LOCKED Workers, 202 Contract) | ✅ Completed |
| **Phase 4** | Document Text Extraction (Native PDF parsing + OCR fallback) | ⏳ Next |
| **Phase 5** | Chunking, Entity Normalization & Vector Indexing (Qdrant) | 📋 Planned |
| **Phase 6** | Permission Governance, Hard Pre-Filtering & RBAC | 📋 Planned |
| **Phase 7** | Text-to-SQL Engine & Sovereign RAG Query Layer | 📋 Planned |

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
# Starts PostgreSQL, MinIO, API, and Background Worker
docker compose up -d

# Run database schema migrations
alembic upgrade head
```

### 4. Interactive Endpoints
- **API Documentation (Swagger)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Check**: [http://localhost:8000/health](http://localhost:8000/health)
- **Upload Document (Async 202)**: `POST /api/v1/documents/upload`
- **Query Status**: `GET /api/v1/documents/{document_id}/status`

---

## 🧪 Testing & Verification

```bash
# Run the complete test suite (47 tests: 28 unit + 19 PostgreSQL integration, ~7.0s)
pytest tests/ -v

# Run linter
ruff check src/ tests/ main.py
```
