# A-PAG AI Platform

> A secure, scalable AI knowledge and data platform for the **Air Pollution Action Group (A-PAG)**, providing employees with a single natural-language interface to query organizational documents and structured data.

---

## 🎯 What We Are Building

A unified AI system that handles two primary types of questions:

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

## 🏗️ Core Architecture & Pipelines

### 1. Document Ingestion & Processing Pipeline
An asynchronous, multi-stage pipeline designed for safety, deduplication, and accuracy:

```text
PDF Upload ──► Quarantine ──► 8-Point Validation & Scan ──► Raw Storage
                                                                 │
  Knowledge Base ◄── Qdrant Index ◄── Chunking ◄── Normalization ◄── Extraction (Native + OCR)
```

- **Quarantine-First Isolation**: Files land in temporary storage before validation.
- **Fail-Fast Security**: Strict checks for MIME type, 100MB ceiling, header/trailer integrity, and malware.
- **SHA-256 Deduplication & Versioning**: Prevents redundant storage and tracks historical policy versions (`supersedes_id`).
- **Extraction with OCR Fallback**: Native PDF text extraction with automatic OCR fallback for scanned pages.
- **Structured Normalization**: Cleans text, extracts tables (Markdown/HTML), and detects entities (CAQM, GRAP, CPCB, pollutants).

### 2. Text-to-SQL Engine
- Converts natural-language questions (e.g., *"How much was spent on Project X last year?"*) into SQL.
- Provides only relevant schema context to the model (data minimization).
- Validates SQL syntax and ensures strict **read-only execution** against PostgreSQL.

### 3. Permission-Aware Security & Data Sovereignty
- **Hard Pre-Filtering**: Permissions are applied directly during the vector/database search—restricted data is never loaded or retrieved.
- **Classification Gate**: Mandatory check before any document is indexed into the searchable knowledge base.
- **Sovereign Inference**: Evaluated with India-based models (Sarvam AI) with zero-data-retention and data minimization principles.

---

## 💾 Storage Architecture

| System | Role | Contents |
|---|---|---|
| **PostgreSQL** | Relational Database | Document metadata, background job queues, audit trails, and structured operational data. |
| **MinIO** | Object Storage | PDF artifacts across buckets (`quarantine/`, `raw/`, `extracted/`, `normalized/`). |
| **Qdrant** | Vector Database | Document embeddings, chunk payloads, and permission metadata for semantic search. |

---

## 🚀 Quick Start Guide

### 1. Prerequisites
- **Python 3.12+**
- **Docker & Docker Compose**

### 2. Setup & Start the Testing Studio UI
```bash
# Clone repository
git clone https://github.com/AirPollutinActionGroup/A-PAG-AI-KNOWLEDGE-BASE.git
cd A-PAG-AI-KNOWLEDGE-BASE

# Setup virtual environment & dependencies
python -m venv .venv
.venv\Scripts\activate      # Windows (or 'source .venv/bin/activate' on Linux/macOS)
pip install -r requirements.txt

# Start background services (PostgreSQL 16 + MinIO)
docker compose up -d

# Start the Ingestion & Testing Studio server
python main.py
# or:
uvicorn src.api.v1.router:app --reload --host 0.0.0.0 --port 8000
```

### 3. Interactive Web Access
- **Testing Studio UI**: [http://localhost:8000](http://localhost:8000)
  - Features 9 one-click preset test scenarios (Valid V1/V2, Duplicates, Bad Headers, Encrypted PDFs, Exploits, Zero-byte files).
  - Real-time 8-check validation indicator matrix.
  - Ingested documents ledger displaying exact rejection reasons and raw storage paths.
- **Swagger API Docs**: [http://localhost:8000/docs](http://localhost:8000/docs)
- **Health Check Probe**: [http://localhost:8000/health](http://localhost:8000/health)

---

## 🧪 Testing & Verification

```bash
# Run the complete test suite (22 tests, ~2.0s)
pytest tests/ -v

# Run code linter
ruff check src/ tests/ main.py
```
*(Detailed test descriptions and execution logs available in [`TEST_REPORT.md`](TEST_REPORT.md))*

