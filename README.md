# A-PAG AI Knowledge Base Platform

> A governed RAG system for the Air Pollution Action Group: documents go in, and questions come back as **cited answers**, with sensitive details masked before anything leaves the deployment. Ingestion and retrieval are built and measured; Text-to-SQL is not built yet.

---

## 🎯 What We Are Building

A unified AI system that handles two kinds of organizational question:

1. **Document knowledge (RAG)** — policy and project questions answered from PDF, Word, Excel and PowerPoint files, with a citation for every claim. **Built.**
2. **Structured data (Text-to-SQL)** — financial and operational metrics from PostgreSQL via validated natural-language-to-SQL. **Not built yet** (see the roadmap).

```text
                         User Question
                               │
                         AI Chat Layer
                               │
                      Intent Query Routing   ← planned: routes between the two paths
                     /                    \
                    /                      \
                   ↓                        ↓
        Knowledge Base (RAG) ✅        PostgreSQL (Text-to-SQL) 📋
                   │                                │
     Hybrid search: vector + BM25              SQL generation
     (PostgreSQL: pgvector + pg_search)        (read-only DB)
                   │                                │
             Cross-encoder rerank                   │
                   │                                │
         Data Boundary Gateway                      │
         (classify → mask → record)                 │
                   │                                │
            Sarvam writes the answer                │
                   │                                │
                   └───────────────┬────────────────┘
                                   ↓
                      Cited, validated response
```

### What works today

| Capability | State |
|---|---|
| Upload PDF / DOCX / XLSX / PPTX, validated and quarantined first | ✅ |
| Text extraction, with **OCR for scanned PDF pages** | ✅ |
| Structure-aware chunking and local embeddings (no data leaves for indexing) | ✅ |
| Hybrid search (vector + BM25), reranked, with permissions enforced in SQL | ✅ |
| Written answers with citations; **declines** rather than inventing | ✅ |
| **Masking** of phone numbers, emails, Aadhaar, PAN, GSTIN, cards, IFSC before any external model sees text | ✅ |
| JWT auth, owner-scoped `RESTRICTED` documents, reversible delete, append-only audit log | ✅ |
| Measured retrieval and answer quality (see Evaluation) | ✅ |
| Deployed on an Azure VM (see `DEPLOY.md`) | ✅ test deployment |
| Text-to-SQL, Hindi, per-document ACLs, SSO | 📋 not built |

---

## 🏗️ Core Ingestion Architecture (Asynchronous)

Ingestion runs asynchronously to keep the API responsive, isolate CPU-heavy work, and survive service restarts. Processes never call each other; they communicate only through the `documents` and `jobs` tables in Postgres.

```text
Upload ──► FastAPI (POST /upload) ──► Quarantine Storage + Postgres (documents + jobs + audit)
                  │                                                          │
             202 Accepted (<500ms)                                           │
                                                                             ▼
                                                               ScanWorker Daemon
                                                     (SELECT ... FOR UPDATE SKIP LOCKED)
                                                                             │
                                          Validation ladder + heuristic threat scan + SHA-256
                                                                             │
                                                        ┌────────────────────┴────────────────────┐
                                                        ▼                                         ▼
                                                [Validation Passed]                       [Threat/Corrupt/Duplicate]
                                                        │                                         │
                                                Promote to Raw Bucket                     Purge Quarantine
                                                Set Status VALIDATED                      Set REJECTED / DUPLICATE
                                                        │
                                                        ▼
                                              ExtractionWorker Daemon
                              (pdfplumber / python-docx / python-pptx / openpyxl,
                               plus OCR for PDF pages that have no text layer)
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
                Set Status AWAITING_CLASS    (e.g. LOW_TEXT_DENSITY)
                            │
                            ▼
                    ChunkingWorker Daemon
            (split at the document's own headings; tables kept whole;
             sized in model tokens via the tokenizer alone)
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

- **Quarantine-first isolation**: files land in untrusted storage before any parsing or scanning.
- **Immediate 202 Accepted**: the API returns the document UUID, status URL and correlation ID in under 500ms. A document is *searchable* only once it reaches `LIVE`, which is after the embedding stage, so a busy queue means a wait.
- **SKIP LOCKED worker pool**: workers claim jobs without blocking, with 60s leases and a dead-worker reaper. One process per stage (`WORKER_STAGE=SCAN|EXTRACT|NORMALIZE|CHUNK|EMBED`), each its own container off the same image. Postgres is the queue rather than RabbitMQ/Redis, so job state and document state commit in one transaction and cannot drift.
- **SHA-256 deduplication** with a partial unique index, which is the real guarantee under concurrency.
- **Append-only audit trail**: every lifecycle event is logged with a correlation ID, and database triggers forbid `UPDATE`/`DELETE` on it.
- **Text extraction is non-ML**: it reads structure each format already states (Word styles, slide titles, worksheet grids). **OCR** (RapidOCR, ONNX, no PyTorch) is the one exception and runs per page, only where a PDF page has almost no text *and* has images on it. See `KNOWN_DEBTS.md` #13–14.
- **Structure-aware chunking**: passages are cut at the document's own headings, so a citation points at a whole idea and not a clause severed from its condition. Tables become row groups that repeat their header. Chunk size is measured in the embedding model's own tokens.

---

## 🖥️ Interfaces

| Path | What it is |
|---|---|
| `/search` | **Knowledge Base UI** — sign in, ask questions, upload documents inline, preview documents, scope a question to one document ("Ask about this"), and open **"What happened"** on any answer to see what was masked and what was sent where. Conversations stay in your own browser. |
| `/` | Studio UI — upload and watch the pipeline (predates auth and multi-file; see `KNOWN_DEBTS.md` #7). |
| `/docs` | Swagger. |

The sign-in page can show a shared **demo login** beneath the form. It is off unless `DEMO_LOGIN_EMAIL` and `DEMO_LOGIN_PASSWORD` are set in a deployment's `.env`, is never stored in the repository, and refuses to reveal an `ADMIN` account.

---

## 🔎 Hybrid Search and Answers

### Retrieval

`GET /api/v1/search?q=<question>&limit=10` returns the passages that best match, each with the citation recovered at extraction time.

**Two searches run and their ranks are merged.** Vector search matches meaning but blurs exact identifiers (an embedding places "Section 114" near whatever it resembles). **BM25** via `pg_search` matches those exactly and is blind to paraphrase. They fail differently, so fusing them (Reciprocal Rank Fusion) covers more than either. The BM25 query has function words trimmed, because on a natural-language question a dozen common words buried the one rare term that identifies the answer.

A **cross-encoder reranker** then reads the question and each candidate together and reorders them. It matters because the passage that best answers a question often sits well below the top by fusion (ranks 9, 16 and 23 on three sample questions). Reranking scores `RERANK_CANDIDATES` passages (50 by default, 25 on a 2-vCPU VM) and returns the best `limit`.

**Scores are never compared, only ranks.** `score` is not a similarity: read `semantic_rank`, `lexical_rank` and `fusion_rank` instead. `mode=semantic|lexical` runs one arm alone, which is how a surprising result gets explained.

**Permissions are filtered in SQL, before `ORDER BY`/`LIMIT`, in every arm.** A `RESTRICTED` passage removed *after* ranking has already taken a slot, so `limit=5` would quietly return four results with no way to tell whether the corpus is thin or an answer was withheld.

`GET /api/v1/documents/search` is separate: it matches literal words in a document's title, filename and description, not its body.

### Written answers

`POST /api/v1/ask?q=<question>` retrieves passages and has Sarvam (`sarvam-105b-conversations`) write an answer from them, citing passages as `[1]`, `[2]`.

- **It declines rather than invents.** Vector search always returns *something*, so a similarity gate withholds passages that do not answer the question, and the model is instructed to say when the passages do not cover it. The gate catches off-topic questions; plausible-sounding fabricated ones are caught by the model, which is why both layers are measured.
- **Scoping.** `document_id` restricts a question to named documents. A pronoun ("this document") is never resolved by guessing.
- **Follow-ups** ("what about Category B?") are handled by query expansion for the search only, and the expanded query is shown so a search that quietly looked for something else is visible.

### The Data Boundary Gateway

Nothing reaches an external model except through `src/modules/gateway/`. Three steps:

1. **Classify.** A request takes the highest tier present across its passages. Anything not explicitly `PUBLIC` is withheld, including a missing or unrecognised tier. If nothing survives, no call is made.
2. **Mask.** Deterministic patterns replace GSTIN, PAN, Aadhaar (Verhoeff checksum), cards (Luhn), IFSC, email, Indian mobile and landline numbers with typed placeholders such as `<PHONE_1>`. Checksums are what keep a 12-digit emissions figure from reading as an identity number. Detection is patterns, not a model, so **a person's name in prose is not detected**.
3. **Record.** A record is written on every crossing, including when nothing was found, so "nothing sensitive" is distinguishable from "the scan never ran".

Placeholders deliberately survive into the answer so a reader sees where a real value was withheld.

---

## 📊 Evaluation

Three harnesses, none of which need a judge model unless stated.

| Harness | Measures | Run |
|---|---|---|
| `run_eval.py` | Retrieval: does the right document reach the model | `python run_eval.py --compare` |
| `run_quality_suite.py` | Refusal, citations, masking, isolation, consistency, answer fidelity | `python run_quality_suite.py` |
| `run_ragas.py` | Answer faithfulness (needs a judge with credit; never Sarvam) | `python run_ragas.py --count 25` |

Retrieval, on 100 questions generated from real passages (`eval_set.json`):

| configuration | hit@1 | hit@5 | MRR |
|---|---|---|---|
| **hybrid + rerank (shipped)** | **83.0%** | **97.0%** | 0.894 |
| hybrid, no rerank | 80.0% | 91.0% | 0.853 |
| semantic only | 72.0% | 90.0% | 0.801 |
| lexical only (BM25) | 66.0% | 89.0% | 0.751 |

Reranking at 25 candidates (the 2-vCPU setting) measures 82.0% / 95.0% and is about 1.8× faster. The three questions that still miss are prose, two of them asking *why* something is the case, which is the harder retrieval problem.

Answer fidelity: every number in an answer is checked against the passages it was built from. Over 30 answers: **100% numeric fidelity (73 of 73 figures)** and 90% citation coverage. It cannot see a claim that is wrong without being numerically wrong, and says so.

**Read these as what they are.** `eval_set.json` is a draft generated from the corpus, with `expected_answer` blank on purpose; colleagues adding the questions they actually ask is the most valuable review. The retrieval figures measure whether the right *document* was found, not whether the answer was right. Latency varies a lot by machine, so the ratios between rows are more reliable than the seconds.

---

## 💾 Storage Architecture

| System | Role | Contents |
|---|---|---|
| **PostgreSQL 16 (ParadeDB image: pgvector + pg_search)** | Relational, vector **and** BM25 database | Document metadata, users, job queues, audit logs, and the retrievable passages in `document_chunks` with their `vector(768)` embeddings (HNSW, cosine) and a BM25 index. |
| **MinIO** | Object storage | Document artifacts across buckets (`quarantine/`, `raw/`, `extracted/`, `normalized/`). |

There is deliberately **no separate vector store**. A passage's text, its citation metadata (`page_number`, `section_heading`, `is_table`) and its embedding live in one row, so one query returns the answer, what to cite, and enforces the permission rule. Splitting the vector into another system would mean resolving ids across two stores and applying permissions after the top-k was already chosen.

Documents stay on the deployment's own disk. Nothing is sent to a cloud storage service; the only data that leaves is the masked, `PUBLIC`-only passages sent to Sarvam when someone asks a question.

---

## ⚠️ Current Limitations

- **Open registration**: `POST /auth/register` has no invite gate, which is acceptable only while the API is not exposed beyond a trusted network. A registrant is always a plain `USER`; a role cannot be supplied. See `KNOWN_DEBTS.md`.
- **Classification is required at upload** (`PUBLIC` or `RESTRICTED`), with no default, because it decides what may be sent to an external model. `RESTRICTED` means the uploader plus any `ADMIN`; there are no per-document ACLs. See `ARCHITECTURE.md` §6b.
- **Names are not masked.** The gateway detects patterned identifiers; a person's name in prose has no pattern. Presidio with NER is the upgrade path.
- **OCR is a fallback, not a parser.** It returns text lines, so a scanned *table* comes out as ungrouped numbers, and it drops word boundaries, which costs the keyword search a term it can never match.
- **English-only embeddings**: `BAAI/bge-base-en-v1.5` cannot embed Devanagari, so a document detected as non-English stops at `SKIPPED_UNSUPPORTED_LANGUAGE` and is recorded as a queryable backlog rather than indexed as noise (~10–15% of A-PAG's corpus is Hindi). Hindi is deliberately not built.
- **Vector width is fixed at migration time**: swapping to a model of a different width needs a migration plus a full re-embed, not a config edit.
- **A few chunks exceed the model window**: 5 of 4,459 (0.11%): four are single table rows wider than the window, emitted whole by design, and one is prose. Such a chunk is embedded from its first 512 tokens.
- **Malware scanning is a heuristic signature check**, not ClamAV (see `KNOWN_DEBTS.md` #8). This fits a trusted-uploader threat model and should be revisited before any wider rollout.
- **Single-tenant, single-VM deployment** with no automatic backups.
- **Embedding is the slow stage on a small VM.** A bulk ingest on 2 vCPUs takes on the order of an hour; set `INFERENCE_THREADS` to the vCPU count (see `DEPLOY.md`).

### Supported upload formats

| Format | Extension | Unit count recorded |
|---|---|---|
| PDF | `.pdf` | pages |
| Word | `.docx` | — (Word text reflows; there is no page count until the document is rendered) |
| Excel | `.xlsx` | worksheets |
| PowerPoint | `.pptx` | slides |

The format is resolved from the file's **contents**, not its name or the MIME type the browser declares, so a `.docx` reported as `application/octet-stream` still uploads, and a spreadsheet renamed to `.docx` is stored as the spreadsheet it is.

Not accepted, with the reason:

- **Macro-enabled files** (`.docm`/`.xlsm`/`.pptm`, or any file containing a macro project) — re-save without macros. A downloaded Office file does eventually get opened by a person, and that executes macros.
- **Legacy or password-protected Office files** (`.doc`/`.xls`/`.ppt`, encrypted `.docx`) — re-save in the modern format, or remove the password.
- **Google Docs/Sheets/Slides** — these live in Drive and have no bytes to upload. Either use *File → Download → Microsoft Excel (.xlsx)* (or Word/PowerPoint) and upload the result, or put the file in the shared Drive folder and let `drive_sync.py` export it for you — it converts Docs, Sheets and Slides to docx, xlsx and pptx for exactly this reason.
- **CSV** — out of scope for now; it has no container structure to validate and carries a different (formula-injection) risk profile.

---

## 🗺️ Roadmap & Phase Status

| Phase | Description | Status |
|---|---|---|
| **Phase 1–3** | Ingestion, quarantine, validation, deduplication, storage promotion, immutable audit log | ✅ Completed |
| **Phase C** | Asynchronous architecture (SKIP LOCKED workers, 202 contract) | ✅ Completed |
| **Phase 4** | Text extraction, including per-page OCR for scanned PDFs | ✅ Completed |
| **Phase 5** | Normalization (cleaning, language detection, quality gate) | ✅ Completed |
| **Phase 6** | Chunking, embedding, hybrid retrieval (vector + BM25), reranking, SQL-level permissions | ✅ Completed |
| **Phase 7a** | Data Boundary Gateway (classify, mask, record) and cited answer generation | ✅ Completed |
| **Phase 7b** | Evaluation harnesses (retrieval, behaviour, fidelity, RAGAS) | ✅ Completed |
| **Phase 7c** | Test deployment on Azure (`DEPLOY.md`) | ✅ Completed |
| **Phase 7d** | Invite-only registration or SSO, person-name masking, backups, TLS | 📋 Planned |
| **Phase 8** | Text-to-SQL engine, intent routing, certified metrics agreed with department heads | 📋 Planned |
| **Phase 7e** | Google Drive connector — shared folder sets the tier, `drive_sync.py` imports it through the same pipeline | ✅ Command built; scheduled worker planned |
| Later | Supersede/versioning, freshness warnings, semantic cache | 📋 Designed, not built |

---

## 🚀 Quick Start Guide

### 1. Prerequisites
- **Python 3.12+**
- **Docker & Docker Compose**

### 2. Environment Configuration
```bash
cp .env.example .env
```
Set `SARVAM_API_KEY` to enable written answers. Without it search still works and `/ask` returns 503, which is a reduced service and not a crash.

### 3. Start Infrastructure & Background Services
```bash
# PostgreSQL (ParadeDB), MinIO, the API, and one worker container per pipeline stage
docker compose up -d

# Run database schema migrations
alembic upgrade head
```

### 4. Use it
- **Knowledge Base UI**: [http://localhost:8000/search](http://localhost:8000/search)
- **API documentation (Swagger)**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health check**: [http://localhost:8000/health](http://localhost:8000/health)

Create an account with `POST /api/v1/auth/register`, then sign in with `POST /api/v1/auth/login` (OAuth2 password flow: form fields `username` = email, `password`).

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/documents/upload` | Upload 1–10 files (multipart `files`, with a `classification`); returns 202 |
| `GET /api/v1/documents/{id}/status` | Pipeline status of one document |
| `GET /api/v1/documents?limit=&offset=` | Paginated list |
| `GET /api/v1/documents/search?q=` | Title/filename/description search |
| `GET /api/v1/search?q=` | Hybrid, reranked passage search |
| `POST /api/v1/ask?q=` | Cited written answer |
| `POST /api/v1/documents/{id}/classify` | Change a document's tier (owner or admin; audited) |
| `DELETE /api/v1/documents/{id}` | Reversible delete; `?permanent=true` is admin-only |

All `/api/v1/documents/*`, `/search` and `/ask` endpoints require `Authorization: Bearer <token>`.

### 5. Deploy
`DEPLOY.md` is the runbook for an Azure VM, written for a 2-person test box: sizing (8 GB RAM is the floor), the firewall allow-list, a Docker network setting Azure needs, generating secrets, and the settings that matter on a small machine (`RERANK_CANDIDATES`, `INFERENCE_THREADS`, `EMBEDDING_MEM_LIMIT`).

---

## 🧪 Testing & Verification

```bash
# Full suite: unit tests plus Postgres integration tests against a throwaway container
pytest tests/ -v

# Unit tests only (in-memory repository, no Docker needed)
pytest tests/unit -v

# Lint: this exact file list is what CI runs
ruff check src/ tests/ main.py worker_main.py

# Evaluation
python run_eval.py --compare
python run_quality_suite.py
```

Integration tests build their schema by running the real Alembic migrations, not `create_all()`, because much of this schema's behaviour (audit immutability triggers, the dedup index, the BM25 index) lives in migrations and not in the ORM. They skip, and never fall back to a real database, if Docker is unavailable.

See `CLAUDE.md` for the full architecture notes, `ARCHITECTURE.md` for design decisions, and `KNOWN_DEBTS.md` for what was deliberately deferred and why.
