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
                            │                  a scanned document reached the system)
                            ▼
                    ChunkingWorker Daemon
            (split at the document's own headings; tables kept whole)
                            │
                Write rows to document_chunks
                Set Status CHUNKED
                            │
                            ▼
                    EmbeddingWorker Daemon
              (BAAI/bge-base-en-v1.5 via fastembed, ONNX on CPU)
                            │
            ┌───────────────┼───────────────┐
            ▼               ▼               ▼
    [Vectors written] [Non-English]   [Inference error]
            │               │               │
    embedding column   Set Status      Set Status
    populated (768)    SKIPPED_UNSUP   EMBEDDING_FAILED
    Set Status LIVE    PORTED_LANGUAGE
```

- **Quarantine-First Isolation**: Files land in isolated temporary storage before any parsing or scanning.
- **Immediate 202 Accepted**: API returns document UUID, status URL, and correlation ID in under 500ms.
- **SKIP LOCKED Worker Pool**: Background workers pull jobs without blocking, managed by 60s leases and a 30s dead-worker reaper. One worker process per pipeline stage (`WORKER_STAGE=SCAN|EXTRACT|NORMALIZE|CHUNK|EMBED`), each its own container off the same image. Postgres is the queue rather than RabbitMQ/Redis — job state and document state then commit in the same transaction and can never drift, which matters more at this scale than a throughput ceiling nothing approaches.
- **SHA-256 Deduplication & Partial Unique Index**: Hardware-accelerated hashing prevents duplicate storage while permitting superseded version history.
- **Append-Only Audit Trail**: Every document lifecycle event is immutably logged with correlation IDs.
- **Non-ML text extraction**: reads structure each format already states (Word styles, slide titles, worksheet grids) rather than inferring it — no OCR, no layout-inference model. See Current Limitations below.
- **Structure-aware chunking**: passages are cut at the document's own headings, not at fixed intervals, so a citation points at a whole idea rather than a clause severed from the condition qualifying it. Tables become row groups that repeat their header.
- **Embeddings on the chunk row**: `document_chunks.embedding` is a `vector(768)` beside the text it encodes, indexed with HNSW (cosine). The model runs locally on CPU and is baked into the image — nothing leaves the deployment, and no weights are fetched at boot.

---

## 🖥️ Interfaces

| Path | What it is |
|---|---|
| `/search` | **Search UI** — ask the corpus a question, get ranked passages with citations and a live token readout. Sign in with the same account as the API. |
| `/` | Studio UI — upload and watch the ingestion pipeline (predates auth/multi-file, see `KNOWN_DEBTS.md` #7). |
| `/docs` | Swagger. |

---

## 🔎 Hybrid Search

`GET /api/v1/search?q=<question>&limit=10` returns the passages whose *meaning* is closest to the
question, each with the citation recovered at extraction time:

```json
{
  "query": "how is machine learning model training evaluated",
  "count": 2,
  "results": [
    { "filename": "Hands-On-Machine-Learning.pdf", "page_number": 221,
      "section_heading": "Chapter 4. Training Models", "is_table": false,
      "score": 0.699, "text": "Chapter 4. Training Models  So far we have treated …" },
    { "filename": "Hands-On-Machine-Learning.pdf", "page_number": 154,
      "section_heading": "Better Evaluation Using Cross-Validation", "is_table": false,
      "score": 0.689, "text": "One way to evaluate the decision tree model would be …" }
  ]
}
```

**Two searches run and their ranks are merged.** Vector search matches meaning but blurs exact
identifiers — an embedding places "Section 114" near whatever it is semantically similar to. A
full-text index matches those exactly and is in turn blind to paraphrase. They fail differently,
so fusing them covers more than either: on a 16-query sample the word search surfaced passages the
vector search never returned on 6 of them. Asked for `cuDNN`, the vector arm's top hit was the
book's *Index* page; the word arm found the actual content.

Scores are never compared, only ranks (Reciprocal Rank Fusion) — cosine similarity and `ts_rank`
are not on a common scale and no conversion between them exists. **So `score` is not a
similarity**: read `semantic_rank`/`lexical_rank` instead. `mode=semantic|lexical` runs one arm
alone, which is how a surprising result gets explained.

This is distinct from `GET /api/v1/documents/search`, which matches literal words in a document's
**title, filename and description** — body text is not in that index at all.

**Permissions are filtered in SQL, before `ORDER BY`/`LIMIT`.** This is a correctness requirement,
not tidiness: a `RESTRICTED` passage removed *after* ranking has already won its slot, so a
`limit=5` would quietly return four results — or none — with no way for the caller to tell whether
the corpus is thin or an answer was withheld.

**Every response carries a `usage` block** — query tokens, context tokens, the model's window, and
how many returned passages were truncated by it. Context size is what decides whether these
passages fit in a future LLM prompt, so it is worth watching now, while chunk sizing can still be
changed cheaply. A passage longer than the window was embedded only up to the cap: the text is
stored whole but the vector is not, so something mentioned only in its tail cannot be found.
2.9% of the current corpus is in that state — see `KNOWN_DEBTS.md` #28.

---

## 💾 Storage Architecture

| System | Role | Contents |
|---|---|---|
| **PostgreSQL 16 + pgvector** | Relational **and** vector database | Document metadata (incl. full-text search index), users, background job queues, audit logs, and — since Stage 7 — the retrievable passages in `document_chunks` together with their `vector(768)` embeddings and HNSW index. |
| **MinIO** | Object Storage | Document artifacts across buckets (`quarantine/`, `raw/`, `extracted/`, `normalized/`). |

There is deliberately **no separate vector store**. A passage's text, its citation metadata
(`page_number`, `section_heading`, `is_table`) and its embedding live in one row, so a similarity
search returns the answer *and* what to cite *and* enforces the permission rule in a single query.
Splitting the vector into a second system would mean fetching ids from one store and resolving them
in another — two round trips, and the permission check applied after the top-k was already chosen.
The Postgres image is therefore `pgvector/pgvector:pg16` rather than stock `postgres:16-alpine`.

---

## ⚠️ Current Limitations

- **Open registration**: `POST /auth/register` has no invite gate yet — acceptable only for the internal, not-internet-exposed bootstrap phase. See `KNOWN_DEBTS.md`.
- **2-tier classification only**: `RESTRICTED` = uploader + ADMIN (owner-scoped, not department-based); no per-document ACLs. See `ARCHITECTURE.md` §6b.
- **Single-Tenant Deployment**: Multi-organization partitioning is deferred to later milestones.
- **No OCR**: Pipeline implements Stages 1–7 (quarantine → validation → promotion → text extraction → normalization → chunking → embedding). Text extraction is deliberate and non-ML — it reads structure each format already states rather than inferring it — and there is no OCR fallback, since this corpus is digitally authored, not scanned. A document with no real text layer is stopped at `NORMALIZATION_FAILED` (`LOW_TEXT_DENSITY`/`EMPTY_TEXT`) rather than silently indexed empty. See `KNOWN_DEBTS.md` #13–14.
- **English-only embeddings**: `BAAI/bge-base-en-v1.5` is an English model, so a document normalization detected as non-English stops at `SKIPPED_UNSUPPORTED_LANGUAGE` instead of being embedded. This is not a failure — an English tokenizer turns Devanagari into unknown tokens and emits vectors that match nothing, which would leave the document sitting in the index invisible with no signal it is missing. Skipped documents are a queryable backlog for the multilingual phase (~10–15% of A-PAG's corpus is Hindi). Set `EMBEDDING_SKIP_NON_ENGLISH=false` once a multilingual model is configured.
- **Vector width is fixed at migration time**: `EMBEDDING_DIMENSIONS` must match the migrated `vector(N)` column (`alembic check` enforces this). Swapping to a model of a different width is a migration **plus a full re-embed of the corpus**, not a config edit. Chunking is deliberately a separate stage so that re-embed never requires re-chunking.

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
| **Phase 6b** | Embedding & Vector Indexing (pgvector, self-hosted model) | ✅ Completed |
| **Phase 7** | Permission Governance, Hard Pre-Filtering & RBAC | 📋 Planned |
| **Phase 6c** | Retrieval endpoint (similarity search with SQL-level permission filtering) | ✅ Completed |
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
# Starts PostgreSQL (pgvector build), MinIO, API, and one worker container per pipeline
# stage (scan, extraction, normalization, chunking, embedding)
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
# Run the complete test suite (244 tests: 211 unit + 33 PostgreSQL integration, ~25s)
pytest tests/ -v

# Run linter
ruff check src/ tests/ main.py
```
