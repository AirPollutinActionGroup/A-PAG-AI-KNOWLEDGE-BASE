# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A-PAG AI Knowledge Base: an async document ingestion pipeline (Python 3.12, FastAPI) that will
eventually feed a governed RAG platform (hybrid search over PDF/DOCX/XLSX/PPTX) plus a Text-to-SQL layer over
PostgreSQL. **Currently implemented: the whole ingestion pipeline** (upload/quarantine → validate, scan and
promote → extract → normalize/quality gate → chunk → embed),
plus JWT auth, multi-file upload, pagination, full-text search, and **semantic search**
(`GET /api/v1/search`) over the pgvector index. Text-to-SQL is not yet built (see Roadmap in
README.md).

## Commands

```bash
# Environment
cp .env.example .env

# Start infra + API + workers (Postgres, MinIO, api, and one container per stage:
# scan, extract, normalize, chunk, embed)
docker compose up -d

# Run DB migrations
alembic upgrade head

# Run app locally (outside docker)
python main.py            # FastAPI on :8000
python worker_main.py      # runs one stage, selected by WORKER_STAGE (default SCAN)
WORKER_STAGE=EXTRACT python worker_main.py
WORKER_STAGE=NORMALIZE python worker_main.py
WORKER_STAGE=CHUNK python worker_main.py
WORKER_STAGE=EMBED python worker_main.py

# Enqueue jobs for documents stranded before a stage existed (idempotent)
python backfill_jobs.py --stage EMBED --dry-run
python backfill_jobs.py --stage EMBED

# Re-run a stage over documents it already finished, for when the *stage* improved rather than
# the document being stranded (--reset moves them back to the stage's entry status first)
python backfill_jobs.py --stage EXTRACT --status NORMALIZATION_FAILED --reset --dry-run

# Tests
pytest tests/ -v                              # full suite (unit + Postgres integration)
pytest tests/unit -v                          # unit tests only (in-memory repo, no DB needed)
pytest tests/integration -v                   # integration tests (throwaway Postgres via testcontainers; SKIPS if Docker is down — never silently falls back to DATABASE_URL, since these tests DELETE whole tables)
pytest tests/unit/test_ingestion_pipeline.py::TestName::test_case -v   # single test

# Lint
ruff check src/ tests/ main.py worker_main.py backfill_jobs.py
```

Interactive endpoints once running: Swagger at `/docs`, health at `/health`, Studio UI at `/`
(note: Studio UI predates auth/multi-file and still expects a synchronous single-file response —
see `KNOWN_DEBTS.md` #7). All `/api/v1/documents/*` endpoints require `Authorization: Bearer <token>`
— get one via `POST /api/v1/auth/register` then `POST /api/v1/auth/login`.

## Architecture

### One API entrypoint, one worker entrypoint that runs any stage

- `main.py` → FastAPI app (`src/api/v1/router.py`) — handles `POST /documents/upload` (fast path
  only, returns `202` in <500ms) and `GET /documents/{id}/status`.
- `worker_main.py` → reads `WORKER_STAGE` (`SCAN` default) and runs the matching `BaseWorker`
  subclass — `ScanWorker`, `ExtractionWorker`, `NormalizationWorker`, `ChunkingWorker`, or
  `EmbeddingWorker`. One process handles one
  stage; each stage runs as its own container in Docker Compose off the same image (see
  `docker-compose.yml`), because the container healthcheck watches a single heartbeat file, which
  assumes one worker daemon per container (`KNOWN_DEBTS.md` #5).

Processes never call into each other directly; they communicate only through the `documents` and
`jobs` tables in Postgres. When changing pipeline behavior, check both the fast-path call site
(`UploadService.receive()`, or the previous stage's handler enqueuing the next job) and the heavy
path (the stage's own `*JobHandler.process()`).

### Pipeline stages (by module)

Numbered to match `JobStage`, which is the vocabulary the `jobs` table and `worker_main.py` use.
Stage 1 is the synchronous upload path and has no job of its own — it *creates* the first one:

| # | `JobStage` | Module |
|---|---|---|
| 1 | — (the HTTP request) | `upload_service.py` |
| 2 | `SCAN` | `scan_job_handler.py` |
| 3 | `EXTRACT` | `extraction_job_handler.py` |
| 4 | `NORMALIZE` | `normalization_job_handler.py` |
| 5 | `CHUNK` | `chunking_job_handler.py` |
| 6 | `EMBED` | `embedding_job_handler.py` |


1. **Receive/Quarantine** — `src/modules/document_pipeline/upload_service.py`
   `UploadService.receive()`: writes bytes to the `quarantine/` bucket, inserts a `Document` row
   (status `QUARANTINED`) and a `Job` row (`stage=SCAN`, `status=PENDING`) in the *same* request,
   then returns 202 immediately. Never runs validation itself.
2. **Validate/Scan/Promote** — `src/modules/document_pipeline/scan_job_handler.py`
   `ScanJobHandler.process()`: fetches the document, re-reads bytes from quarantine, runs
   `ValidationService` (`src/modules/document_pipeline/validation.py` — a fail-fast ladder: size,
   format lookup, container check, heuristic threat scan, deep structural check, SHA-256 — a
   decompression-bomb ratio check was tried and removed, see `KNOWN_DEBTS.md` #9), then either
   rejects (purges quarantine object, sets `REJECTED`) or promotes (copies to `raw/` bucket keyed
   by SHA-256, sets `VALIDATED`, enqueues an `EXTRACT` job). Dedup and promotion are guarded by an
   in-process lock (`self._promotion_lock`) plus a DB partial unique index
   (`uq_documents_active_sha256`) as the real guarantee under multi-worker concurrency. Handler
   methods are idempotent — re-running `process()` on an already-finalized (or already-promoted)
   document is a safe no-op that returns the existing state (important since jobs can be
   retried/reaped).
3. **Extract** — `src/modules/document_pipeline/extraction_job_handler.py`
   `ExtractionJobHandler.process()`: fetches a `VALIDATED` document, reads its `raw/` bytes, and
   dispatches to the `TextExtractor` registered for its MIME type
   (`src/modules/document_pipeline/extraction/extractors.py` — one per format, deliberately
   non-ML; see the "Text extraction" section below). Writes the result to
   `extracted/{document_id}.json`, sets `EXTRACTED`, enqueues a `NORMALIZE` job. A file that can't
   be parsed sets `EXTRACTION_FAILED` and stops the pipeline there — no job is queued after a
   failure at any stage.
4. **Normalize** — `src/modules/document_pipeline/normalization_job_handler.py`
   `NormalizationJobHandler.process()`: fetches an `EXTRACTED` document, reads its
   `extracted/{id}.json`, and runs `NormalizationService` (`src/modules/document_pipeline/normalization/`
   — text cleaning, language detection, then a quality gate). This stage never opens the original
   file and has no per-format branching — structure (headings/tables) is carried through from
   extraction, not re-derived. A passing document is written to `normalized/{id}.json` and set to
   `AWAITING_CLASSIFICATION` — which now means what it says (normalization succeeded), not "just
   promoted" as it did before this stage existed. A quality-gate failure sets
   `NORMALIZATION_FAILED` and is never retried (the content was read correctly; retrying produces
   the identical verdict). On success a `CHUNK` job is enqueued — the sensitivity tier is chosen
   by the uploader at upload rather than confirmed at a gate, so nothing here waits on a human.
5. **Chunk** — `src/modules/document_pipeline/chunking_job_handler.py`
   `ChunkingJobHandler.process()`: fetches an `AWAITING_CLASSIFICATION` document, reads its
   `normalized/{id}.json`, and splits it into retrievable passages via `ChunkingService`
   (`src/modules/document_pipeline/chunking/` — see "Chunking" below). Passages are written to
   the `document_chunks` **table**, not a bucket, and the document is set `CHUNKED`. Re-running
   replaces a document's chunks rather than appending, so a reaped or retried job cannot double
   its content. On success an `EMBED` job is enqueued.
6. **Embed** — `src/modules/document_pipeline/embedding_job_handler.py`
   `EmbeddingJobHandler.process()`: fetches a `CHUNKED` document, reads its `document_chunks` rows,
   and writes a `vector(768)` back onto each one via `EmbeddingService`
   (`src/modules/document_pipeline/embedding/` — see "Embedding" below). The document becomes
   `LIVE`, which now means what it says: every passage carries a vector and is retrievable.
   Nothing is queued after this stage — it is the end of ingestion.

### How a stage runs: the claim loop

Not a pipeline stage — the engine every stage runs on, which is why it carries no number and
no `JobStage` value.

`src/workers/base_worker.py` (`BaseWorker`): generic SKIP LOCKED job-queue engine
(polling, atomic claim via `UPDATE ... WHERE ... FOR UPDATE SKIP LOCKED RETURNING`, lease
expiry, exponential-backoff retries, a periodic reaper for stuck/expired leases, and a
heartbeat file for container healthchecks). Each stage's worker (`ScanWorker`,
`ExtractionWorker`, `NormalizationWorker`, `ChunkingWorker`, `EmbeddingWorker`, in
`src/workers/`) subclasses it and only implements
`process_job()`, which opens a session and delegates to that stage's job handler. A new
pipeline stage follows the same pattern: subclass `BaseWorker`, implement `process_job()`,
raise `TransientProcessingError` vs `PermanentProcessingError` (`src/core/errors.py`) to
control retry vs. immediate-fail behavior, and register the worker class in `worker_main.py`'s
`WORKERS` dict.

### Text extraction: deliberately non-ML

`src/modules/document_pipeline/extraction/extractors.py` reads what each format already states
rather than inferring it: Word paragraph styles are headings, slide titles are headings, worksheets
are already grids. PDF is the only format with no semantic structure, so it gets pdfplumber's
ruling-line table detection plus a font-size heading heuristic (weighted by character count, not
line count — see the code comment on why a line-count tie can eat a document's only heading).
Docling (the design docs' choice, and still current best practice for self-hosted layout parsing)
was evaluated and rejected, then **re-evaluated when the original reason expired** — the corpus
turned out not to be uniformly typed. The rejection survived on narrower grounds: a layout parser
earns its cost on scanned tables, and these scans have none. See `KNOWN_DEBTS.md` #13 for the
measurement and the trigger to revisit; the `TextExtractor` ABC and `EXTRACTORS` registry exist so
swapping one format's extractor is a one-line registry change, not a pipeline rewrite, and Docling
takes RapidOCR as its own OCR backend so today's layer would sit underneath it rather than be
discarded.

**OCR** (`extraction/ocr.py`) is the one exception, added because the corpus disagreed with the
assumption above. Three of the first 39 real documents are scans with no text layer at all —
including a 162-page set of CPCB directions — and they extracted to **zero characters** and
stopped at `NORMALIZATION_FAILED`. That is the quality gate working (`LOW_TEXT_DENSITY` /
`EMPTY_TEXT` in `normalization/quality_checker.py`, scoped to PDF since it's the only format that
can be a scan): the gap was recorded and findable rather than silent, which is why the check was
built before the capability existed.

The fallback is decided **per page, not per document** — a government PDF is routinely a typed
covering letter with a scanned annexure behind it, and a per-document switch loses one half
whichever way it is set. A page qualifies when it has almost no text *and* has images on it; both
halves matter, or a typed corpus pays ~3.5s a page for nothing, or every blank separator page
costs seconds to confirm it is blank. `ExtractedUnit.method` records `NATIVE` or `OCR` per page
and `ExtractionResult.resolve_method()` derives the document-level `NATIVE`/`OCR`/`MIXED` from
them, so the two cannot disagree.

RapidOCR (ONNX) rather than Docling or a hosted parser, and the reasoning is in `KNOWN_DEBTS.md`
#13: a layout parser earns its ~500MB and PyTorch on scanned **tables**, and these scans measured
zero table structure — they are prose. LlamaParse and unstructured's API tier would send every
document out of the deployment at ingestion, which cannot be reconciled with redacting anything
before it leaves. `rapidocr` runs on the onnxruntime already in the image for embeddings, and
rasterisation needs nothing new since pdfplumber already depends on pypdfium2.

Two things to know before changing it. **Thread count is not a tuning detail**: left at the
onnxruntime default these pages measured 20.5s each against 3.5s at 8 intra-op threads, so
`OCR_THREADS` is set explicitly. And **OCR drops word boundaries** (`FGDinexisting plantswere`),
which costs the lexical arm a term it can never match — BM25 cannot match what was never
tokenised. Lines below `OCR_MIN_CONFIDENCE` are dropped rather than kept, because an invented
word inside a passage that will later be cited is worse than a gap.

### Chunking: structure-aware, and why chunks live in Postgres

`src/modules/document_pipeline/chunking/chunker.py` cuts a document at its own headings rather
than at fixed intervals. Chunk boundaries decide two things at once — what the embedding model
sees as one idea, and what a citation can point at — so a fixed-size cut through the middle of a
clause separates an obligation from the condition qualifying it, and the answer that results is
confidently wrong while still carrying a citation. The structure is free: extraction already
recovered it from each format's own declarations, and this stage never re-derives it.

Oversized sections fall back to recursive splitting (paragraph → sentence → hard wrap), and the
pieces are repacked toward the target so a split section doesn't shatter into one-sentence
fragments. A hard wrap derives its width from the ratio measured on *that* text rather than a
global constant — the lesson of #28 being that a ratio holding for prose does not hold for a
table of numbers. Tables are never split mid-row; a large table becomes row groups that each
**repeat the header**, because otherwise group 7 of a budget sheet is a wall of numbers with no
column names. An oversized group is re-split **by rows**, never by characters, since
hard-wrapping a table produces exactly the fragment the repeated header exists to prevent; a
single row too wide for the budget is emitted whole for the same reason. There is deliberately **no overlap** — a Jan 2026 analysis found it adds indexing cost
with no measurable recall gain, and that holds doubly when splitting on real heading boundaries.

Sizing is delegated to a `SizeBudget` (`chunking/sizing.py`) and measured in **tokens**, via
`TokenBudget` built from the configured model's tokenizer. It used to be measured in characters,
on the reasoning that the tokenizer belongs to the embedding model and that arrives a stage
later. That proxy held near the corpus average and broke outside it: 2.9% of chunks overflowed
the 512-token window and were silently truncated at embed time, **every one of them inside the
character budget** (`KNOWN_DEBTS.md` #28). Cut points were never the problem; the budget could not
see what it constrained.

`TokenBudget` caps at `min(CHUNK_MAX_TOKENS, model window)`, so neither a generous setting nor a
model swap to a larger window (BGE-M3's is 8192) can produce a chunk the model would truncate, or
one so large that precision suffers. `CharacterBudget` remains for callers with no tokenizer —
unit tests, and a worker whose model fails to load, which logs a WARNING because it quietly
reintroduces the bug. `TokenCounter` is a narrow Protocol satisfied structurally by
`EmbeddingProvider`, so chunking depends on *tokenization*, not on embedding; the coupling was
always there (2000 chars was chosen with a 512-token window in mind) and writing it as a character
constant hid it rather than removing it.

The cost: `ChunkingWorker` loads the embedding model purely to tokenize (~640MB), which is why
that container has a `mem_limit`. See `KNOWN_DEBTS.md` #32 for the lighter option and why it was
not taken.

`document_chunks` is the one pipeline artifact that lives in Postgres instead of object storage.
That's deliberate: chunks are queried, not merely stored — the embedding stage adds a vector
column to the same row, and a similarity search cannot join against JSON in a bucket.
`page_number`, `section_heading` and `is_table` are the citation contract, and the `scale` column
ships holding one value so a second granularity later is an INSERT rather than a migration plus a
backfill (`KNOWN_DEBTS.md` #17).

### Embedding: the model is a configuration decision, the dimension is not

`src/modules/document_pipeline/embedding/` mirrors `chunking/`'s shape — pure logic, no storage or
DB awareness. `provider.py` holds the `EmbeddingProvider` ABC and `FastEmbedProvider` (ONNX via
fastembed, no PyTorch); `service.py` batches chunks per inference call.

`embed_passages()` and `embed_query()` are **separate methods**, and that split is load-bearing.
Several model families are asymmetric — E5 requires literal `query: `/`passage: ` prefixes, BGE
v1.5 takes an instruction on the query side only — and feeding both sides the same shape costs
retrieval quality with **no exception and no warning**. Keeping that inside the provider means the
retrieval endpoint cannot forget it and a model-family change edits one class, not every call site.
`_is_bge()` deliberately excludes BGE-M3, which is not instruction-tuned the way v1.5 is.

The **model** is configuration (`EMBEDDING_MODEL`); the **dimension** is not.
`document_chunks.embedding` is `vector(768)` fixed by migration `0014`, and pgvector columns are
typed by width, so a model of a different width needs a migration *and* a full re-embed — vectors
from two models do not share a space. Three guards, outermost first: `alembic check` fails if
`settings.EMBEDDING_DIMENSIONS` and the migration drift apart; `FastEmbedProvider` probes the
model's real output width at load and refuses to start on a mismatch (so it surfaces at worker boot,
not as a wall of failed jobs); the column width itself is the backstop. See `KNOWN_DEBTS.md` #21.

Because chunking is its own stage, a model swap re-runs **one worker** over existing
`document_chunks` rows — it never re-chunks or re-extracts.

The model is **baked into the image** (`Dockerfile` sets `FASTEMBED_CACHE_PATH` and pre-fetches the
weights at build time), not downloaded at boot: a worker that fetches weights on first start fails
closed on a network blip and is not reproducible.

**Language gate**: normalization already detected a language, so `EmbeddingJobHandler` reads it and
routes a non-English document to `SKIPPED_UNSUPPORTED_LANGUAGE` rather than embedding it. This is
not a failure state and is never retried. An English tokenizer turns Devanagari into unknown tokens
and emits vectors that match nothing — the document would sit in the index permanently unfindable
with nothing to explain it. Same reasoning as the quality gate's `LOW_TEXT_DENSITY` check: a
recorded gap is findable, a silent one is not. `EMBEDDING_SKIP_NON_ENGLISH=false` disables it when a
multilingual model is configured — config, not code (`KNOWN_DEBTS.md` #20).

Transient vs permanent follows the usual split: model load and memory pressure are transient (retry
is likely to work); a dimension mismatch or a `CHUNKED` document with no chunks is permanent
(retrying produces the identical error).

**Token accounting.** `count_tokens()` measures true length through a tokenizer with **both
truncation and padding disabled**: truncation would cap every answer at the window so overflow —
the only thing worth measuring — is invisible, and `encode_batch` pads to the longest text in the
batch so every passage in a batch reports an identical length (that one shipped briefly, and was
caught by a live response where five passages all claimed 689 tokens — `KNOWN_DEBTS.md` #29).

This is what chunking now sizes against, so **no chunk exceeds the window**: verified 0 of 2,860
after the fix, down from 83 of 2,816 when the budget was in characters (`KNOWN_DEBTS.md` #28).
`EmbeddingService` still logs a WARNING and records `truncated_count` if one ever does, because
the guarantee depends on the chunking stage having a tokenizer — a worker that falls back to
`CharacterBudget` silently reintroduces the gap.

**Retrieval filters permissions in SQL**, via `visible_documents_clause()` from
`src/modules/auth/access.py` — the same predicate list and search use. A RESTRICTED chunk filtered
post-hoc still consumes a top-k slot and pushes out a result the user was allowed to see, so the
predicate belongs in the `WHERE` clause, before `ORDER BY`/`LIMIT`. The shape is pinned by
`tests/integration/test_vector_search.py` and exercised through the shipped query by
`tests/integration/test_retrieval_service.py`.

### Retrieval: hybrid, fused by rank, permissions inside every arm

`src/modules/retrieval/` is what reads the index. Two arms run and their **ranks** are fused:

- **semantic** — `embed_query()` (never `embed_passages()`, see the Embedding section), ranked by
  pgvector's `<=>` cosine distance, the operator the HNSW index was built for. Any other distance
  function returns correct results as a sequential scan over the whole corpus.
- **lexical** — **BM25** via `pg_search` (ParadeDB), migration `0016`. Indexed over both `text`
  and `section_heading`, queried with `paradedb.boolean(should => [match(text), match(heading)])`
  and ranked by `paradedb.score()`.

  This replaced a `tsvector`/`ts_rank` arm (`0015`), and the reason was **recall, not ranking**.
  `websearch_to_tsquery` builds a conjunction — every term must appear in the same chunk — which
  on this corpus returned **0** rows for "penalties for non-compliance", 1 for "air quality
  targets" and 2 for "enforcement obligations": precisely the multi-word policy questions the
  system exists to answer. BM25 scores partial matches and returned a full page for all three.
  Across 16 sample queries, the arm went from silent on 2 of them to contributing on all 16.
  It also drops `ts_rank`'s length bias (no document-length normalisation): same queries,
  `ts_rank` returned chunks averaging 1,237 characters against BM25's 892.

  Both fields are searched because only 1,056 of 2,475 headed chunks repeat their heading in the
  body — `@@@` applied to a single column searches that column alone, which silently ignored
  headings until it was measured. `paradedb.match()` is used rather than interpolating the query
  into pg_search's syntax, so a question containing a colon, a quote or the word "OR" cannot be
  reinterpreted as operators.

  The `0015` tsvector column and trigger are **still present and now unread** — kept so the switch
  stays revertible. Removing them is the immediate follow-up (`KNOWN_DEBTS.md` #35).

They fail differently — an embedding blurs "Section 114" into whatever it is semantically near,
while a word index is blind to paraphrase — which is why fusing beats either. Measured on this
corpus: on 6 of 16 sample queries the lexical arm surfaced passages the vector arm never returned.
For `cuDNN` the semantic arm's top hit was the book's *Index* page; the lexical arm found the
actual content.

**Scores are never compared, only ranks** (`fusion.py`). Cosine similarity is bounded and means
"close in meaning"; `ts_rank` is unbounded, length-dependent, and means "these words match". The
conversion factor between them does not exist, and any constant chosen for one corpus quietly
stops being right for another. RRF (`k=60`, Cormack et al. 2009) uses `1/(k+rank)` per arm. The
consequence to know: `RetrievedChunk.score` is **not a similarity** — it is ~0.016 for a single
first place and is meaningless outside one result set. Read `semantic_rank`/`lexical_rank` instead,
which is also what the UI shows.

The join between arms is a **FULL OUTER JOIN**. An inner join would reduce hybrid to "what both
arms agree on" — narrower than either arm alone, the opposite of the intent. Each arm fetches
`candidate_pool(limit)` rows so fusion has material; ordering breaks ties on `chunk_id` so a
repeated query returns a repeated order.

`src/api/v1/retrieval.py` exposes `GET /api/v1/search`. The provider is built once per process
behind `get_retrieval_service()` — a FastAPI dependency rather than a module-level singleton, so
tests can override it and so importing the module does not load weights. Note this makes the API
process carry the model (~640MB resident) in addition to the embedding worker.

**The permission predicate is in the `WHERE` clause of *every arm*, before `ORDER BY`/`LIMIT`.**
For top-k this is a correctness requirement, not tidiness — a RESTRICTED chunk removed after
ranking has already taken its slot, so `limit=5` returns four results, or none, and the caller
cannot tell whether the corpus is thin or an answer was withheld. Hybrid doubled the places it can
leak from, and a predicate present in the semantic arm but missing from the lexical one would pass
every test written for the former; `tests/integration/test_hybrid_search.py` covers both. The
predicate comes from `src/modules/auth/access.py` rather than being written here, so retrieval and
the document list/search cannot drift on what "visible" means.

Identity comes from `get_current_user`, never from the request — there is a test asserting a
`user_id` query parameter is ignored.

**Reranking** (`retrieval/rerank.py`) runs between the SQL and the result list: the arms fetch
`RERANK_CANDIDATES` (50) rows, a cross-encoder scores each against the query, and the best
`limit` are returned. The wider fetch is the point and not an implementation detail — reranking
cannot recover a row the SQL never selected. Measured on this corpus, the passage that most
directly answered a question sat at fused rank **9, 16 and 23** on three sample questions;
with `limit=8` none of them would have been shown at all.

Both retrieval arms score a passage *without ever looking at the query and the passage together*
— the semantic arm compares two independently-computed vectors, BM25 counts term overlap. A
cross-encoder reads the pair in one pass, which is why it finds what fusion ranked 23rd, and also
why it cannot replace retrieval: scoring every chunk against every query is quadratic.

`rerank_score` is an unbounded logit on the model's own scale — like `score`, it orders and does
not measure. `fusion_rank` carries the pre-rerank position, which is what the UI shows
("moved up from #23"). Passages are truncated to `RERANK_MAX_CHARS` **for scoring only**; the
full text is still what is returned and cited. Reranking happens *after* the permission
predicate, never before — a cross-encoder must not be shown a passage its caller may not see.

A missing or broken reranker degrades to the fused order rather than failing the search, and
logs at ERROR: it improves results that are already useful, so losing it should cost the best
ordering and not the answer. `RERANK_ENABLED=false` switches it off entirely; the test suite
sets that by default (`tests/conftest.py`) so unrelated tests do not download an 80MB model.

**Grounding.** Vector search always returns something: there is no such thing as no nearest
neighbour. Without a check, a question about cake comes back with the nearest policy passage, a
page citation and every appearance of confidence. `assess()` gates on raw **cosine similarity**,
not the RRF score — the score is built from ranks and cannot tell "best in the corpus" from "best
of a bad lot", whereas similarity is absolute and comparable across queries. Below
`SEARCH_MIN_SIMILARITY` the endpoint returns `grounded=false` and **withholds the passages**
rather than flagging them, because a citation beside text that does not answer the question is
how someone ends up quoting something irrelevant in a government submission. An exact lexical
match counts as grounded whatever the similarity: if the words are literally in a document, the
corpus contains them. The 0.55 default was measured on this corpus (on-topic 0.69–0.84,
off-topic 0.45–0.50) and is **model-specific** — re-measure it on a model change.

### Storage: 4-bucket + repository abstraction

- `src/storage/object_storage.py` defines `ObjectStorage` (abstract) with `LocalFileSystemStorage`
  (dev, writes under `./storage_data/`) and `MinIOStorage` (prod) implementations, selected via
  `BucketManager` per `settings.STORAGE_BACKEND`. Buckets: `quarantine/` (untrusted, purged after
  promotion or rejection) → `raw/` (validated bytes, permanent) → `extracted/` (per-document
  `{id}.json`, the raw extraction result) → `normalized/` (per-document `{id}.json`, cleaned +
  quality-gated — the artifact the chunking stage reads). `extracted/`/`normalized/` keys
  are deterministic (`storage_keys.py`'s `extraction_key_for`/`normalized_key_for`), not
  content-addressed like `raw/` — two documents with identical bytes were already deduplicated at
  promotion, so keying by document_id needs no lookup for a handler to find its own output.
- `src/modules/document_pipeline/repository.py` defines `DocumentRepository` (abstract) with
  `PostgreSQLDocumentRepository` (production, wraps a SQLAlchemy `Session`) and
  `InMemoryDocumentRepository` (unit tests / concurrency tests, no DB needed). **Every** handler
  takes a repository via constructor injection — `UploadService` and all five `*JobHandler`
  classes; none reaches for a `Session` directly for document state. Prefer adding new methods to
  the abstract base and both implementations together, not special-casing one.

### Audit trail

`src/modules/audit/service.py` (`AuditService.log_event`) writes append-only rows to `audit_log`
(DB-level triggers forbid UPDATE/DELETE — migration `0003_revoke_audit_log_writes.py`). Every
state transition should call `self._audit(...)` (both `UploadService` and `ScanJobHandler` have a
private `_audit()` wrapper) with a `correlation_id` threaded through the whole request/job
lifecycle. Every handler has that wrapper — `UploadService` and all five `*JobHandler` classes
— so a new stage is expected to as well. Audit writes are **best-effort, not transactional** with
the state change — a failure is
logged at ERROR but never blocks the pipeline (deliberate trade-off, see `KNOWN_DEBTS.md` #1). The
worker-side `_audit()` additionally de-dupes by checking for an existing `(document_id,
event_type)` row first, since jobs can be retried.

### Auth & access control

- `src/modules/auth/` — `security.py` hashes passwords with `bcrypt` directly (not `passlib`,
  which is unmaintained and incompatible with `bcrypt>=4.1` — see `ARCHITECTURE.md` §6a) and
  issues/decodes JWTs; `service.py` (`AuthService`) does registration/authentication against the
  `users` ORM table directly (no repository abstraction — see its module docstring for why);
  `dependencies.py::get_current_user` is the **single seam** every endpoint depends on for "who is
  calling this" — when SSO replaces JWT login later, only this function's internals change.
- `src/api/v1/auth.py` exposes `/auth/register`, `/auth/login` (OAuth2 password flow: form fields
  `username`/`password`), `/auth/me`.
- Access model: `Classification.PUBLIC` (org-wide) vs `RESTRICTED` (owner-scoped: visible to
  `doc.owner_id == current_user.user_id`, plus any `ADMIN`). The rule lives **once**, in
  `src/modules/auth/access.py`, in two forms that are proven equivalent by
  `tests/unit/test_access_rule.py`: `can_view()` for a document already in hand (used by
  `_can_view()` in `src/api/v1/ingestion.py` for single-document endpoints) and
  `visible_documents_clause()` for queries. **Anything returning a result set must use the SQL
  form** — list, search and retrieval all pass `viewer_id`/`viewer_is_admin` into the query.
  Those are required keyword arguments with no default, so forgetting them is a `TypeError`
  rather than a silent leak or a silent empty page (`KNOWN_DEBTS.md` #27). Deliberately a 2-tier
  model, not per-document ACLs — see `ARCHITECTURE.md` §6b before adding per-document or
  department-based sharing (a prior department-tier attempt shipped without departments ever
  being populated, making RESTRICTED admin-only in practice — see `KNOWN_DEBTS.md`).
- Rate limiting: `slowapi` on `POST /documents/upload` (`settings.UPLOAD_RATE_LIMIT`), wired via
  `app.state.limiter` in `src/api/v1/router.py`.

### Supported formats

`src/modules/document_pipeline/formats.py` is the single registry of accepted formats (PDF, DOCX,
XLSX, PPTX). **Adding a format means two registry entries and no edits to the stages themselves**:
a `FormatSpec` in `FORMATS` here, and a `TextExtractor` in `EXTRACTORS`
(`extraction/extractors.py`). Adding only the first gets the file accepted and promoted and then
killed at `EXTRACTION_FAILED`, which reads as a parser bug rather than a missing registration.
The validator owns the shared ladder; the spec owns everything format-specific (extension, magic
bytes, identifying zip part, unit counting, and the two check callables).
`storage_keys.py` derives object-key extensions from the same registry, so there is one source of
truth rather than a second map to keep in sync.

Two structural checks exist, not four: PDF has its own binary layout, while DOCX/XLSX/PPTX are all
Office Open XML — identical ZIP containers differing only by which XML part they carry. The three
OOXML specs therefore share one pair of check functions and differ only by data.

Each spec carries **two** callables, and the split is load-bearing: `container_check` (cheap, no
parsing) runs *before* the threat scan so a disguised binary is reported as corrupt, and
`structural_check` (deep parse, produces the unit count) runs *after* it so a file carrying a known
malicious signature is reported as malicious rather than as whatever the parser chokes on first.
Collapsing these into one call silently reclassifies malicious uploads — there is a regression test
for this (`test_reject_structural_threats`).

Format is resolved from file **content** via `detect_format()` at the API boundary, not from the
declared MIME type (browsers send `application/octet-stream` for valid Office files), and the
worker independently re-verifies content against the stored type before promoting.

`page_count` is really a per-format "unit count": pages for PDF, worksheets for XLSX, slides for
PPTX, and `None` for DOCX — Word text reflows, so a page count doesn't exist until the document is
rendered, and faking one from `app.xml` would be wrong. The column is nullable for this reason.

### Config & enums

- All runtime config is centralized in `src/core/config.py` (`Settings`, pydantic-settings, reads
  `.env`). Add new tunables here rather than reading `os.environ` directly.
- All lifecycle/status/event vocab lives in `src/db/enums.py` (`DocumentStatus`, `Classification`,
  `JobStage`, `JobStatus`, `AuditEventType`) — string enums shared between the DTOs
  (`src/modules/document_pipeline/models.py`), the ORM (`src/db/models.py`), and Postgres columns.
  Extend enums here rather than introducing new string literals.

### DB migrations

Alembic migrations live in `src/db/migrations/versions/`, numbered sequentially. Notable ones:
`0003` revokes UPDATE/DELETE on `audit_log` at the DB level (immutability enforced by the
database, not just application code); `0004` adds the partial unique index on
`documents(sha256)` excluding `SUPERSEDED/ARCHIVED/REJECTED` (the real dedup guarantee, since
application-level checks alone race under concurrency); `0006_users_departments` adds `users` and
document ownership/metadata columns (`title`, `description`, `mime_type`, `page_count`,
`upload_batch_id`, `deleted_at`), a real FK on `uploader_user_id`, and a trigger-maintained
`search_vector` (tsvector) for full-text search — its `upgrade()` checks for existing
tables/columns/constraints first, since integration test fixtures calling
`Base.metadata.create_all()` can otherwise race ahead of Alembic in dev. `0006` also added a
`departments` table and `department_id` columns; `0007_owner_scoped_access` drops all of that
plus `documents.doc_type` — departments were never populated, which made department-scoped
RESTRICTED visibility effectively admin-only, and `doc_type` (a content category) can't be
derived from a file and nothing read it. Access control is now owner-scoped — see
`ARCHITECTURE.md` §6b.

`0011_extract_normalize_statuses` widens `chk_documents_status` (adds `EXTRACTED`,
`EXTRACTION_FAILED`, `NORMALIZATION_FAILED`) and `chk_audit_log_event_type` (adds the matching
`EXTRACTION_*`/`NORMALIZATION_*` event types) for the extract and normalize stages. It repurposes the existing
`VALIDATED` status rather than adding a new one — defined since `0002`, never assigned by any
code before this.

`0012_reclassification` adds the `DOCUMENT_RECLASSIFIED` audit event and
`documents.document_date` (the date printed on the document, as distinct from `created_at` which
is upload time — without it a 2019 policy ingested today looks current). `0013_document_chunks`
adds the `document_chunks` table plus the `CHUNKED`/`CHUNKING_FAILED` statuses and the `CHUNK` job
stage. `0014_embeddings` runs `CREATE EXTENSION IF NOT EXISTS vector`, adds
`document_chunks.embedding vector(768)` and its HNSW index (`m=16`, `ef_construction=64`, cosine),
and adds the `EMBED` stage with `EMBEDDING_FAILED`/`SKIPPED_UNSUPPORTED_LANGUAGE`. The index is
built **normally, not CONCURRENTLY** — `CREATE INDEX CONCURRENTLY` cannot run inside a transaction
and Alembic wraps migrations in one; the table is near-empty at migration time so a blocking build
is instant, but a later rebuild on a populated table must use CONCURRENTLY or it locks out writes
for the duration. Its `downgrade()` resets `LIVE` documents to `CHUNKED` (with the column gone a
LIVE document has no vectors and is not searchable, so leaving it LIVE would be a lie) and leaves
the `vector` extension installed, since other objects may depend on it. Both leave `chk_audit_log_event_type` widened on downgrade rather than narrowing it:
Postgres validates existing rows when creating a CHECK, so narrowing would mean deleting audit
rows, and migration `0003` makes that log append-only precisely so it cannot be rewritten. One
spare value in a typo guard is cheaper than a hole in the audit trail.

**Revision id length**: `alembic_version.version_num` defaults to `VARCHAR(32)` — keep every new
revision id ≤32 characters, or the final version-bump statement fails and rolls back the entire
migration transaction (this happened during `0006`'s development, and again with
`0011_extract_normalize_statuses`, whose first attempt at a fully descriptive id was 38
characters).

### Testing conventions

- `tests/unit/` — use `InMemoryDocumentRepository`, no DB/Docker required; exercise pipeline logic
  and worker mechanics directly.
- `tests/integration/` — real PostgreSQL via `testcontainers`. The schema is built by running
  **the real Alembic migrations**, not `Base.metadata.create_all()`: much of this schema's
  behaviour is not in the ORM (the `audit_log` immutability triggers from `0003`, the partial
  unique index that is the actual dedup guarantee from `0004`, the tsvector triggers from `0006`
  and `0015`), and under `create_all()` none of it exists in the test database — a lexical-search
  test then finds nothing because the trigger was never created, which looks exactly like a broken
  query. `src/db/migrations/env.py` only falls back to `settings.DATABASE_URL` when the caller has
  not set a URL, so the suite's migrations run against the throwaway container and not a real
  database (`tests/integration/conftest.py`;
  the package must be installed — `uv sync` covers this, since it's declared in `pyproject.toml`,
  not just `requirements.txt`). Tests run inside a rolled-back transaction per test (`db_session`
  fixture), but some worker tests (`test_worker_execution.py`, `test_worker_skip_locked.py`)
  `DELETE` the whole `documents`/`jobs` tables outside that transaction to get a clean queue —
  harmless in a disposable container, destructive against a real database. If `testcontainers`
  can't start a container, the suite refuses to fall back to `settings.DATABASE_URL` silently —
  it `pytest.skip()`s with a fix-it message unless `APAG_ALLOW_DESTRUCTIVE_DB_TESTS=1` is set.
  Never set that env var against a database with real data.
- `tests/fixtures/pdfs/` contains real PDF fixtures (valid, corrupted header, truncated EOF,
  disguised binaries, embedded-script malware samples, password-protected) exercised through
  named presets in `GET /documents/test-preset/{preset_name}` for the Studio UI — when adding a new
  validation check, add a matching fixture + preset rather than only unit-testing it in isolation.

## Key trade-offs to know before changing things

These are documented, deliberate decisions — see `ARCHITECTURE.md` and `KNOWN_DEBTS.md` for full
rationale before "fixing" them:

- Workers use **synchronous** SQLAlchemy sessions, not `asyncpg`/async ORM (CPU/IO-bound work gets
  no benefit from asyncio; keeps FastAPI's event loop unstarved).
- Job orchestration is **Postgres `SKIP LOCKED`**, not Kafka/RabbitMQ/Redis — deliberate, scoped to
  A-PAG's expected 50–500 user scale.
- Malware scanning (`ClamAVScanner` in `validation.py`) is a **heuristic signature check only**
  (`/JavaScript`, `/Launch`, `/OpenAction`, EICAR string), not real ClamAV/`clamd`. This is a
  deliberate deferral based on the actual threat model (trusted internal uploaders, PDFs are never
  rendered/executed by this system) — see `KNOWN_DEBTS.md` #8 for the specific triggers that would
  require wiring in real `clamd`. Don't "fix" this without reading that rationale first.
- Idempotency (duplicate job execution, duplicate audit events) is enforced in **application
  logic**, not DB constraints — see `KNOWN_DEBTS.md` #3 before assuming a DB-level guard exists.
  The one exception: concurrent dedup promotion races on `uq_documents_active_sha256` are caught
  as an `IntegrityError` in `ScanJobHandler.process()` and routed to `DUPLICATE` — that DB
  constraint is the real guarantee, application code is just handling its failure mode.
- Auth is **JWT + bcrypt against local Postgres**, not SSO — deliberate for the current
  50-employee, no-existing-SSO phase. See `ARCHITECTURE.md` §6a.
- Registration is **open** (`POST /auth/register` has no invite/admin gate) — deliberate only
  while the API is internal-only. See `KNOWN_DEBTS.md` #0.
- Text extraction is **non-ML** (`pdfplumber`/`python-docx`/`python-pptx`/`openpyxl`), not Docling
  — deliberate given this corpus is typed documents whose structure the file already states, not
  scanned/complex-layout PDFs Docling's layout inference exists for. See `KNOWN_DEBTS.md` #13
  before adding it back; the `TextExtractor` registry is designed for a one-format swap, not a
  wholesale rewrite, if a real document proves the trade-off wrong.
- **OCR exists, for scanned PDF pages only** (`rapidocr`, ONNX, no PyTorch). It was added
  because debt #14's stated trigger fired: three real documents arrived with no text layer. It is
  a *fallback*, not a parser — it returns text lines, not table grids, so a scanned table would
  come out as ungrouped numbers. `OCR_ENABLED=false` switches it off entirely and is
  authoritative even over an injected engine. See `KNOWN_DEBTS.md` #13/#14.
- The Postgres image is **`paradedb/paradedb:0.25.10-pg16`**, not `pgvector/pgvector:pg16`. It
  carries pgvector *and* `pg_search` on the same Postgres 16.15, so the switch was not a version
  upgrade and the data directory was unchanged. `pg_search` must be in
  `shared_preload_libraries`: a fresh container gets that from ParadeDB's own entrypoint, but an
  existing volume initialised by the older image does not, which is why `docker-compose` passes
  it as an explicit `command` flag and CI does not need to.
- Vectors live in **Postgres via pgvector**, not Qdrant/Pinecone/Weaviate — a passage's text, its
  citation metadata and its embedding are one row, so retrieval returns the answer, what to cite,
  and the permission check in a single query. A separate vector store would mean resolving ids
  across two systems and applying permissions *after* top-k was chosen. The Postgres image is
  therefore `pgvector/pgvector:pg16`, not stock `postgres:16-alpine`.
- Embeddings are **English-only** (`BAAI/bge-base-en-v1.5`, 768-dim, CPU/ONNX, baked into the
  image). Non-English documents are recorded as `SKIPPED_UNSUPPORTED_LANGUAGE` rather than embedded
  as noise — ~10–15% of A-PAG's corpus is Hindi, and that backlog is deliberately queryable. See
  `KNOWN_DEBTS.md` #20.
- **Reranking is on by default** and is where most of the retrieval quality now comes from.
  It is also the only per-query CPU cost in the request path, so `RERANK_MODEL` is a latency
  decision as much as a quality one. Unlike the embedding model there is no dimension to match
  and no re-embed to do — changing it changes ordering from the next query onward.
- **Retrieval filters permissions in SQL**, in `src/modules/retrieval/service.py`. Do not copy the
  Python post-filtering in `src/api/v1/ingestion.py`'s list/search — for top-k that is a
  correctness bug, not untidiness (see the Retrieval section).
