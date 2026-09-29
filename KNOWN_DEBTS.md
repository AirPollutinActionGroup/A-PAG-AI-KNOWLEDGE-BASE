# Known Technical Debts

Technical debts and trade-offs tracked deliberately. Each debt is annotated with its rationale, impact, and the specific trigger for when it must be resolved.

---

## 1. Closed Debts (Resolved in Phase C - Async Foundation)

### ✅ Sync architecture limits concurrency (Closed)
- **Resolution**: Refactored ingestion pipeline to asynchronous background execution in Phase C.
- `POST /api/v1/documents/upload` accepts uploads in `<500ms` returning `202 Accepted`.
- Background worker consumes jobs via `SELECT ... FOR UPDATE SKIP LOCKED` with automatic lease management and dead-worker reaper.

### ✅ UploadService monolithic validation/scanning (Closed)
- **Resolution**: Separated into fast-path `UploadService.receive()` (quarantine landing + DB insertion + job enqueue) and standalone `ScanJobHandler.process()` (validation, ClamAV structural threat scanning, deduplication, promotion, and audit logging).

### ✅ No integration tests against real PostgreSQL (Closed)
- **Resolution**: Added 19 PostgreSQL integration tests using `testcontainers-python` verifying `FOR UPDATE SKIP LOCKED` concurrency, partial unique index deduplication, check constraints, lease reclamation, and transaction isolation.

---

### ✅ No user identity, auth, or ownership (Closed)
- **Resolution**: Added a `users` table (migration 0006), JWT-based auth
  (`src/modules/auth/`), and wired `documents.uploader_user_id` + `audit_log.user_id` end-to-end.
  All endpoints now require a bearer token via a single `get_current_user` dependency seam.
  (Migration 0006 also added a `departments` table; it was removed in migration 0007 — see the
  closed debt below.)

### ✅ Concurrent duplicate uploads crash instead of resolving to DUPLICATE (Closed)
- **Resolution**: `ScanJobHandler.process()` now catches the `IntegrityError` from
  `uq_documents_active_sha256` when two workers race to promote the same checksum, and routes
  the loser to `DUPLICATE` instead of letting the job fail and retry 3x.

### ✅ Storage I/O errors misclassified as permanent failures (Closed)
- **Resolution**: `ScanWorker.process_job` now raises `TransientProcessingError` (not
  `PermanentProcessingError`) on `STORAGE_ERROR` — a MinIO blip is retried, matching the
  quarantine-file-preserved-for-retry behavior the handler already implemented.

### ✅ Heuristic scanner false-positived on benign PDFs (Closed)
- **Resolution**: Removed `/OpenAction` (common, benign PDF directive) and bare `/JS` (collides
  with unrelated binary stream bytes) from `ClamAVScanner.MALICIOUS_SIGNATURES`. Removed the
  naive `/Encrypt` byte-scan pre-check in favor of pypdf's authoritative `reader.is_encrypted`.

### ✅ Integration tests silently deleted the dev database's documents (Closed)
- **Resolution**: `testcontainers[postgres]` was listed in `requirements.txt` but missing from
  `pyproject.toml` — the file `uv sync` actually installs from. So the package was never
  installed, and `tests/integration/conftest.py::_get_postgres_url()` caught the resulting
  `ImportError` and silently fell back to `settings.DATABASE_URL` — the real development
  database. Several integration tests (`test_worker_execution.py`,
  `test_worker_skip_locked.py`) do `session.query(DocumentORM).delete()` /
  `session.query(JobORM).delete()` to get a clean queue for each test, which is harmless
  against a disposable container but destroys real data against a dev DB. Every `pytest tests/`
  run was doing this. Fixed by adding `testcontainers[postgres]` to `pyproject.toml` and making
  the fallback refuse to run by default: it now `pytest.skip()`s with instructions instead of
  silently using `DATABASE_URL`, unless `APAG_ALLOW_DESTRUCTIVE_DB_TESTS=1` is explicitly set.
  `audit_log` rows survived throughout (DB-level immutability trigger, migration 0003) — this is
  why the audit history looked intact even as documents kept disappearing.

### ✅ Three-role model (ADMIN/CONTRIBUTOR/VIEWER) implied permissions that didn't exist (Closed)
- **Resolution**: Every permission check in the codebase only ever tested `role == ADMIN` —
  CONTRIBUTOR and VIEWER were never actually distinguished anywhere (both could upload,
  search, download, and delete their own documents identically). Migration
  `0009_collapse_user_roles` collapsed `UserRole` to `ADMIN` / `USER`, converting existing
  CONTRIBUTOR/VIEWER rows to `USER` and updating the `chk_users_role` constraint. The system
  is now honestly a 2-tier model matching what `_can_view()` and the delete endpoint actually
  enforce: ADMIN (sees/deletes every document) vs. USER (manages only their own).
- **Trigger to revisit**: if a real need appears for a role that can upload but not delete, or
  view but not upload, reintroduce a role split *and* wire `require_role()` (already defined
  in `src/modules/auth/dependencies.py`, currently unused) into the relevant endpoints —
  don't add a role value without an enforcement point using it.

### ✅ Departments table existed but was never populated, making RESTRICTED admin-only (Closed)
- **Resolution**: Migration 0006 added `departments` + `users.department_id` +
  `documents.department_id`, and `_can_view()` gated RESTRICTED visibility on department match.
  In practice no departments were ever seeded and no user ever had a `department_id` set, so
  every RESTRICTED document was visible to ADMINs only — including the uploader who set that
  classification. Migration `0007_owner_scoped_access` dropped `departments`,
  `users.department_id`, and `documents.department_id`, and replaced the visibility check with
  `doc.owner_id == current_user.user_id or current_user.role == ADMIN`. See `ARCHITECTURE.md`
  §6b.

### ✅ `documents.doc_type` accepted but never used (Closed)
- **Resolution**: Content category (POLICY/REPORT/DATASET/LEGAL/OTHER) can't be derived from a
  file — it's a document-understanding task, and the pipeline already reserves
  `AWAITING_CLASSIFICATION` for the future stage that will determine it properly. Nothing
  downstream ever read the field. Dropped in migration `0007_owner_scoped_access`; will be
  re-added once real (automatic) classification exists in Phase 4+.

### ✅ BucketManager silently ignored STORAGE_BACKEND=minio (Closed)
- **Resolution**: `BucketManager.__init__` previously defaulted `use_local=True` regardless of
  `settings.STORAGE_BACKEND`, and every default-constructed call site (`UploadService`,
  `ScanJobHandler`, `ScanWorker`, plus the API's shared `_buckets` singleton) never overrode it.
  In the dockerized deployment this meant uploads were silently written to each container's
  ephemeral local filesystem instead of MinIO — invisible in the MinIO console and lost on
  container restart (no volume mounted for `storage_data/`). Fixed by resolving all defaults
  (`use_local`, endpoint, credentials) from `settings` inside `BucketManager` itself. Verified
  live: worker log now reads `Storage backend: MinIO (minio:9000)` and uploaded files appear in
  `mc ls local/apag-raw`.

---

## 2. Active Technical Debts

### 0. Open self-registration (`POST /auth/register` has no gate)
- **Status**: Deliberate for the 50-person internal-org bootstrap phase.
- **Context**: Anyone who can reach the API can create an account with any role, including
  ADMIN. Acceptable only because the API is not internet-exposed yet and the org is small/known.
- **Trigger to address**: Before any external-network exposure, or before onboarding beyond the
  initial trusted cohort — gate behind an admin invite flow or SSO instead.

### 0b. Classification is a 2-tier model (PUBLIC / owner-scoped RESTRICTED), not per-document ACLs
- **Status**: Deliberate scope limit for 50-person scale.
- **Context**: RESTRICTED visibility = uploader + ADMIN role. No department tier, no per-document
  or per-user sharing list exists.
- **Trigger to address**: When a real team/department-based sharing need appears (with real
  departments actually populated and users actually assigned to them — the prior attempt at this
  shipped without either), add a `document_acl` join table or a department tier rather than
  expanding the enum — don't overfit the 2-tier model to a one-off request.

### 1. Audit writes are best-effort, not transactionally guaranteed
- **Status**: Accepted trade-off.
- **Context**: State changes and audit events commit in separate transaction boundaries. If an audit write encounters an unhandled exception, the document state change remains committed while an ERROR log is written.
- **Trigger to address**: When unifying database session orchestration across Phase 4 extraction pipelines.

### 2. Separate-transaction pattern for repository test-doubles
- **Status**: Maintained for in-memory unit tests.
- **Context**: `InMemoryDocumentRepository` does not bind a SQLAlchemy session, necessitating optional `db_session` injection for `AuditService`.
- **Trigger to address**: When all pipeline integration layers standardize exclusively on session-bound repositories.

### 3. Idempotency is application-layer, not DB-enforced
- **Status**: Application-guarded.
- **Context**: `ScanJobHandler` and `ScanWorker` query document status (`doc.status != "QUARANTINED"`) and existing audit records before executing promotions. A race between two workers on duplicate jobs is handled cleanly in application logic, but there is no database-level unique constraint on `(document_id, event_type)` in `audit_log`.
- **Trigger to address**: Before introducing multi-stage pipeline workflows (e.g. OCR/Extraction/Chunking) where pipeline stages can be dynamically retried.

### 4. Retry failure classification is coarse
- **Status**: Two-tier (`TransientProcessingError` vs `PermanentProcessingError`).
- **Context**: Network timeouts and database locks trigger exponential backoff retries, while corrupted PDF syntax and malware trigger immediate `FAILED`/`REJECTED` status. Edge cases (e.g., malformed scanner daemon responses) default to transient retry.
- **Trigger to address**: When production monitoring highlights specific scanner or storage edge cases requiring custom retry policies.

### 5. Single-worker container healthcheck model
- **Status**: Heartbeat file (`/tmp/worker_alive`).
- **Context**: Docker container healthcheck inspects file modification time (`stat -c %Y /tmp/worker_alive < 30s`). This model assumes one worker daemon per container.
- **Trigger to address**: When scaling to multiple worker subprocesses within a single container.

### 6. Observability and queue metrics
- **Status**: Structured application logging only.
- **Context**: Queue depth, job duration, lease renewals, and dead-letter counts are logged at INFO/DEBUG levels, but no Prometheus metrics endpoint is exposed yet.
- **Trigger to address**: Prior to user-facing production release.

### 7. Studio UI expects synchronous upload response
- **Status**: Fast-follow UI task.
- **Context**: The testing Studio UI at `/` was written for the synchronous pipeline and expects terminal `AWAITING_CLASSIFICATION` immediately from `POST /upload`. It needs to be updated to poll `GET /api/v1/documents/{id}/status` every 500ms until terminal state.
- **Trigger to address**: Before internal user onboarding.

### 8. Malware Scanning: Heuristic Only, Real ClamAV Deliberately Deferred
- **Status**: Deliberate architectural deferral.
- **Decision**: After evaluating the actual threat model, we determined real ClamAV-style signature scanning is not justified for Phase 1.
- **Reasoning**:
  - Uploads come from trusted internal employees on company-managed devices, sourced from Drive/email that already passed through upstream malware scanning (Google/email provider).
  - The pipeline never executes or renders PDF content (no PDF viewer, no macro execution, no embedded script execution) — the primary threat ClamAV-style scanning defends against (a viewer executing malicious embedded content) does not apply to how this system processes files.
  - The heuristic signature scanner (`ClamAVScanner` interface) is kept as a low-cost anomaly flag (detects `/JavaScript`, `/Launch`, `/OpenAction` patterns) — not a claim of real malware protection.
  - The two threats that DO apply to this system's actual attack surface — parser crashes from malformed structure, and resource exhaustion from oversized files — are covered by structural validation (checks 1–7) and the 100MB file-size ceiling (check 2). A dedicated decompression-bomb ratio check (formerly check 9) was tried and removed — see debt #9 below.
- **Trigger to revisit and add real ClamAV (`clamd` daemon)**:
  - External (non-employee) users gain upload access
  - Documents get distributed/downloaded in ways this system doesn't control (e.g., users can download and open PDFs in Adobe Reader with macros/JS enabled from within the org)
  - A specific compliance/audit requirement mandates named malware scanning software
  - A-PAG's platform expands to CEGIS/Prosperiti or other orgs with different risk tolerance
- **Estimated effort when triggered**: 4–6 hours (`clamd` Docker service, `pyclamd` client, replace `ClamAVScanner` call site, EICAR test).

### 9. No decompression-bomb protection beyond the 100MB file-size ceiling
- **Status**: Deliberately removed (was check #9 in `ValidationService`), not deferred.
- **Context**: The original check rejected a PDF if any internal stream's decompressed size
  exceeded `MAX_DECOMPRESSION_RATIO` (200x) its compressed size. In practice this produced
  false positives on legitimate documents — ordinary highly-compressible content (a
  solid-fill image, a mostly-blank scanned page, an embedded font's glyph table) routinely
  exceeds 200x compression while expanding to only a few hundred KB to a few MB, which is not
  a resource-exhaustion attack by any reasonable definition. Two mitigation attempts were
  tried and rejected in the same debugging session: (1) requiring a minimum absolute
  decompressed size (5MB) before the ratio could reject — still flagged real files; (2)
  keeping the ratio at 200x with no floor — the original, most false-positive-prone
  behavior. The check was removed entirely rather than continuing to tune it.
- **What still bounds resource use**: the 100MB file-size ceiling (check #2) — the only way a
  single upload can grow beyond that is by expanding after decompression, which is exactly
  the case this debt leaves unguarded.
- **Trigger to revisit**: a real incident (worker OOM/crash from a decompressed stream) — not
  a hypothetical. If revisited, don't reintroduce a compression-ratio comparison; instead cap
  the *absolute* decompressed size of any single stream against a generous, size-independent
  ceiling (e.g. reject only past 1–2GB decompressed), since ratio was the actual source of
  every false positive, not absolute size.

### 10. `db_session`'s rollback doesn't protect against tests that call `.commit()` directly
- **Status**: Worked around per-file, not fixed.
- **Context**: `tests/integration/conftest.py`'s `db_session` fixture wraps each test in a
  transaction and rolls it back at teardown — but that only undoes changes if nothing inside
  the test ever calls `.commit()`. Several tests (`test_worker_execution.py`,
  `test_worker_skip_locked.py`) exercise code paths that commit directly (`ScanJobHandler`,
  raw `session.commit()` calls setting up fixtures), which ends the outer transaction early —
  the rollback at teardown then has nothing left to undo. Both files compensate with their own
  `autouse=True` fixture that manually `DELETE`s the specific tables they touch before each
  test, which fully solves it for those two files but isn't a general guarantee for any new
  integration test that commits.
- **Why not fixed properly**: the real fix is nested `SAVEPOINT`s (what Django's `TestCase`
  does) so an inner `.commit()` releases a savepoint instead of ending the outer transaction.
  That's a real rewire of the session factory used by every repository/service under test, for
  a problem that's already fully contained by the two existing manual-cleanup fixtures.
- **Trigger to address**: if a third integration test file needs to commit and the
  copy-the-cleanup-fixture pattern starts feeling repetitive/error-prone, switch to the
  savepoint approach once, for all files, rather than adding a fourth bespoke `DELETE` fixture.

### 11. OOXML files are validated as containers, not inspected as content
- **Status**: Accepted trade-off, scoped to the current threat model.
- **Context**: `formats.py` validates DOCX/XLSX/PPTX by reading the zip **central directory only**
  (`namelist()`): it confirms the container opens, carries `[Content_Types].xml` and the part that
  identifies its declared type, rejects macro projects (`vbaProject.bin`) and archive entries that
  would escape an extraction root. It deliberately does **not** parse the XML parts, so none of the
  following are inspected: XXE / "billion laughs" entity expansion, DDE fields, remote-template and
  external-workbook references, or embedded OLE objects.
- **Why that's acceptable today**: nothing in the pipeline parses or renders OOXML content — the
  bytes are stored and served back untouched. This is the same reasoning as debt #8, and it holds
  for exactly as long as that stays true. Macro rejection is the one check kept despite it, because
  unlike a PDF the system never opens, a downloaded `.docx` *is* eventually opened in Word by a
  person, and that does execute macros.
- **Trigger to address**: **Phase 4 extraction**, which will parse these XML parts to pull text out
  of them. At that point an XXE-hardened parser (`defusedxml` or equivalent) and a decision on
  external references become prerequisites, not optional — and debt #8's "never rendered or
  executed" premise has to be re-argued rather than inherited.
- **Not a re-litigation of #9**: the total-uncompressed-size guard available from a zip's central
  directory is a different technique from the per-stream expansion-ratio check that was tried and
  removed for PDFs. It is read from declared metadata without decompressing anything, so it has no
  false-positive mode.
- **Update**: the entry-count (10,000) and total-declared-uncompressed-size (500MB) caps described
  above are now implemented in `_ooxml_structure()` (`MAX_OOXML_ENTRIES` /
  `MAX_OOXML_UNCOMPRESSED_BYTES` in `formats.py`), added after an audit found a 6MB `.pptx`
  declaring 50,000 slides validated cleanly. The rest of this debt — XXE/DDE/external-reference
  content inside the XML parts — is unaffected and still open; these caps only bound the
  container's declared size, they don't inspect what's in it.

### 12. Studio UI test presets are PDF-only
- **Status**: Not addressed — noted while fixing unrelated format-validation bugs.
- **Context**: `GET /documents/test-preset/{preset_name}` and the Studio UI's preset buttons only
  serve PDF fixtures (`tests/fixtures/pdfs/`). There's no one-click way to exercise DOCX/XLSX/PPTX
  validation (e.g. a macro-enabled or path-traversal fixture) from the UI the way PDF has.
- **Trigger to address**: Before relying on the Studio UI to demo or manually test the
  DOCX/XLSX/PPTX validation paths — add fixtures under `tests/fixtures/ooxml/` (or generate them
  in-process, as `tests/unit/test_formats.py` already does) and extend `fixture_map`.

### 13. Extraction is non-ML; Docling deferred until a document proves it necessary
- **Status**: Deliberate choice, revisit on evidence.
- **Context**: The design docs specify Docling for layout-aware parsing, and current research
  still rates it the best self-hosted option. It was evaluated and rejected for now: `docling`
  resolves to **78 packages** including `torch`, `torchvision`, `transformers`, `opencv-python`
  and an OCR engine, on a 2 vCPU/8GiB VM already running the whole stack, in a shared worker
  image every container pulls. What it buys is layout inference for complex PDFs and OCR — and
  OCR was separately ruled out (debt #14), while this corpus is typed documents whose structure
  the file already states. The four libraries used instead (`pdfplumber`, `python-docx`,
  `python-pptx`, `openpyxl`) add 8 small MIT/BSD packages and no ML runtime.
- **What this costs**: weaker results on genuinely complex PDFs — multi-column layouts where
  reading order must be inferred, and tables without ruling lines that pdfplumber can't segment.
- **Trigger to address**: a real document that comes out mangled, not a hypothetical. The
  `TextExtractor` ABC and the `EXTRACTORS` registry exist precisely so this is a per-format swap:
  adding a `DoclingExtractor` for `PDF_MIME` alone touches the registry and nothing else — no
  pipeline, handler, or worker changes. If that happens, consider a separate image for the
  extraction worker so the scan worker doesn't carry the ML stack.

### 14. No OCR — scanned documents are flagged, not read
- **Status**: Deliberate, with an explicit detector rather than an assumption.
- **Context**: OCR exists to recover text from pages that have none — scans and photographs.
  A-PAG's documents are digitally authored (Word/Excel/PowerPoint, or PDFs exported from them),
  so every file has a real text layer and the OCR path would never fire. Building it would mean
  carrying an OCR engine for a code path that never runs.
- **Why this is safe to assume**: because the assumption is checked rather than trusted. A file
  with no text layer extracts to near-nothing, and the normalization quality gate stops it at
  `NORMALIZATION_FAILED` with `EMPTY_TEXT` or `LOW_TEXT_DENSITY`, recording character and unit
  counts in the audit trail. A scan cannot silently become an empty document in the knowledge
  base — it fails loudly, naming the check that caught it.
- **Trigger to address**: `LOW_TEXT_DENSITY`/`EMPTY_TEXT` failures appearing for documents people
  actually need searchable. That is the signal that scanned material has entered the corpus, and
  the point to add an OCR extractor behind the same `TextExtractor` interface. Until then the
  absence of those failures is evidence the decision was right.

### 15. Every worker container carries the full application image
- **Status**: Accepted for current scale.
- **Context**: All three stages (SCAN, EXTRACT, NORMALIZE) run the same image, selected by
  `WORKER_STAGE`. The scan worker therefore ships the extraction libraries it never imports, and
  the VM now runs six containers instead of four.
- **Why that's fine today**: the extraction libraries are small and pure-Python-ish, so the image
  grew marginally. The simplicity of one build, one Dockerfile and one CI path is worth more than
  trimming tens of megabytes.
- **Trigger to address**: real memory pressure on the VM, or adopting a heavy extraction
  dependency (debt #13) that would make the shared image genuinely expensive. Either way the fix
  is a second Dockerfile for the extraction worker, not a re-architecture.

### 16. Entity extraction (spaCy) not implemented
- **Status**: Deferred — it serves a feature that doesn't exist yet.
- **Context**: The design docs' normalization stage includes named-entity extraction. It was left
  out because nothing consumes entities: search, chunking and embedding don't need them. The
  features that do — the Compliance Agent that extracts obligations, owners and deadlines, and
  the field-notes agent that turns observations into structured records — are later phases.
- **Trigger to address**: building one of those agents. Note that they are schema-guided
  structured extraction (a defined set of fields pulled from a document), which is a different
  problem from this pipeline's general-purpose "make everything searchable" extraction, and is
  likely better served by an LLM against a schema than by spaCy NER — worth re-evaluating the
  tool at that point rather than inheriting this choice.

### 17. Chunking indexes at one granularity only
- **Status**: Deliberate for now, with the schema already shaped for the fix.
- **Context**: The best chunk size is a property of the *question*, not the document — a penalty
  lookup wants a clause, "what does this directive do" wants a section. Since the question is
  unknown at indexing time, no single size is right, and the current answer is to index at
  several granularities, query all of them, and fuse the results with Reciprocal Rank Fusion.
  Published oracle experiments put the headroom at 20–40% recall, though that is an upper bound
  measured by letting the system peek at the answer; RRF recovers a fraction of it, not all.
- **Why one scale today**: there is no retrieval evaluation — no golden query set, no recall
  metric — so a second scale could not be shown to help. It would cost 2–5x the storage and
  embedding time on a 3.8GB / 2-vCPU VM in exchange for an unmeasurable benefit. Building the
  measurement first is the cheaper order.
- **What was done anyway**: `document_chunks.scale` is in the unique key from the start, holding
  a single value. Adding a granularity later is an INSERT job rather than a migration, a backfill
  and a retrieval rewrite.
- **Trigger to address**: a retrieval evaluation existing, and showing recall misses that a
  different granularity would have caught. Note that our scales should be structural
  (clause → section → document) rather than arbitrary token windows — the structure is already
  captured, and each level matches a unit a person would actually cite.

### 18. Completed jobs are never cleaned up
- **Status**: Invisible today, will not stay that way.
- **Context**: `BaseWorker`'s reaper handles stuck `RUNNING` leases, but nothing ever removes
  `COMPLETED` rows from `jobs`. Every document now produces five of them (SCAN, EXTRACT,
  NORMALIZE, CHUNK, EMBED), so the table grows at five rows per document forever.
- **Why it doesn't hurt yet**: a few hundred documents is a few thousand rows. Postgres does not
  notice.
- **Trigger to address**: the first bulk archive ingest. At the ~5GB corpus A-PAG expects, this
  becomes tens of thousands of dead rows, and the queue table is `UPDATE`-heavy, so the real cost
  is autovacuum pressure on the same database serving application queries. The fix is small — a
  periodic `DELETE FROM jobs WHERE status = 'COMPLETED' AND finished_at < now() - interval '30
  days'` — and is much easier to add before the table is large.

### 19. A table's position within a unit is not recorded
- **Status**: Worked around honestly; the real fix belongs in extraction.
- **Context**: `ExtractedTable` carries only a `unit_index` — which page, slide or worksheet the
  table came from — not where inside it the table sat. For PDF, PPTX and XLSX that is usually
  enough, because a unit is one page or slide. For DOCX it is not: Word has no pages until it is
  rendered, so the whole document is a single unit and every heading and table shares
  `unit_index=1`.
- **What went wrong**: chunking emits a unit's prose before its tables, so `current_heading` had
  already advanced to the document's *last* heading by the time tables were written. Every table
  in a multi-section Word document was therefore cited under the wrong section. Caught by
  inspecting real chunk rows after an end-to-end run — the unit tests passed throughout, because
  they were written against the same mistaken assumption.
- **Current behaviour**: where a unit carries more than one heading, a table's `section_heading`
  is left `NULL` rather than guessed. The page number still stands, so the citation degrades
  instead of disappearing. A wrong citation is worse than an absent one: a reader can act on
  "page 4 of this document" and check; they cannot recover from being told the wrong section.
- **Trigger to address**: when table-level citation precision matters enough to justify it. The
  fix is to record a character offset (or ordinal position among a unit's blocks) on
  `ExtractedTable` at extraction time, which makes attribution exact for every format. That is a
  change to a shipped stage and a re-extraction of the corpus, so it is worth doing once, with
  the embedding work, rather than twice.

### 20. The embedding model is English-only, and ~10–15% of the corpus is not
- **Status**: Deliberate, with the gap recorded rather than hidden.
- **Context**: `BAAI/bge-base-en-v1.5` is an English model. `EmbeddingJobHandler` reads the
  language normalization already detected and routes a non-English document to
  `SKIPPED_UNSUPPORTED_LANGUAGE` instead of embedding it.
- **Why skipping beats embedding anyway**: an English tokenizer turns Devanagari into unknown
  tokens and emits a vector that matches nothing. The document would be *in* the index and
  permanently unfindable, with no error anywhere to explain it. The same reasoning as the quality
  gate's `LOW_TEXT_DENSITY` check: a recorded gap can be found and fixed, a silent one cannot.
- **Current behaviour**: the skip is an audit row (`EMBEDDING_SKIPPED`) carrying the detected
  language, so "which documents are waiting on a multilingual model" has a SQL answer.
- **Trigger to address**: when Hindi retrieval is actually required. The change is configuration
  plus a migration — `EMBEDDING_MODEL` to a multilingual model, `EMBEDDING_SKIP_NON_ENGLISH=false`,
  and, if the new model's width differs, a new `vector(N)` column and a full re-embed. BGE-M3
  (1024-dim) is the standing candidate. Because chunking is a separate stage, a re-embed re-runs
  one worker over existing `document_chunks` rows; it does not re-chunk or re-extract anything.

### 21. Embedding dimension is fixed in a migration, not configuration
- **Status**: Structural, and correct — but worth knowing before a model swap is proposed.
- **Context**: `document_chunks.embedding` is `vector(768)`, fixed by migration `0014`.
  `settings.EMBEDDING_DIMENSIONS` must agree with it, and `alembic check` fails the build if the
  ORM and the migration drift apart.
- **Why it is not a tuning knob**: pgvector columns are typed by width. Changing
  `EMBEDDING_MODEL` to a model of a different dimension without a migration makes every insert
  fail at the database; with a migration, every existing vector is meaningless and must be
  recomputed, because vectors from two different models do not share a space.
- **Defence in depth**: `FastEmbedProvider` probes the model's real output width at load and
  refuses to start if it disagrees with the configured value, so the mismatch surfaces at worker
  boot rather than as a wall of failed jobs. The column width is the backstop if that is bypassed
  (`tests/integration/test_vector_search.py::test_wrong_dimension_is_rejected_by_the_database`).
- **Trigger to address**: none — this is the intended design. Listed so the cost of a model change
  is understood as "migration + full re-embed", not "edit `.env`".

### 22. Documents that predate a stage are stranded until backfilled
- **Status**: Understood, with a tool for it; not automatic.
- **Context**: each stage enqueues the next stage's job on success. A document that came to rest
  before a stage existed therefore has no job for it and never will. There are currently 26
  documents at `AWAITING_CLASSIFICATION` from before extraction/normalization shipped; their
  `CHUNK` jobs fail with `NoSuchKey` because no `normalized/{id}.json` was ever written for them.
- **Why the failure is correct**: the handler treats a missing artifact as transient (storage
  errors usually are), retries three times, then leaves the job `FAILED` with the document's
  status untouched. Nothing is corrupted; the work simply cannot proceed from where it stopped.
- **Current behaviour**: `backfill_jobs.py` enqueues missing jobs for documents in a given state
  and is idempotent. It cannot fix the 26 above on its own, because they need re-running from
  `EXTRACT` while sitting in a status `ExtractionJobHandler` does not accept — that needs a status
  reset first.
- **Trigger to address**: before the bulk archive ingest, since the same situation recurs at every
  future stage boundary. The durable fix is a small reconciliation command that maps a document's
  status to the stage that should own it next and re-queues from there.

### 23. ✅ A slow job outlived its lease and burned retries while succeeding (Closed)
- **Found**: reprocessing A-PAG's own corpus. A 21MB / 1,351-page PDF took **139s** to extract
  against a 60s lease; embedding its ~2,400 chunks took ~9 minutes. The reaper returned both jobs
  to `PENDING` **and incremented `retry_count`** while the worker was succeeding. At
  `MAX_JOB_RETRIES=3`, any document slow enough to be reaped three times would be marked `FAILED`
  with the work still in flight — and with more than one worker per stage, the re-queued job would
  be claimed and processed concurrently by a second worker.
- **Root cause**: the reaper recovers jobs whose worker died holding a lease, but infers death from
  an expired lease. That silently assumes every job finishes inside one lease period, while
  processing time is a function of document size.
- **Fix**: `_LeaseRenewer` in `src/workers/base_worker.py` renews the lease from a daemon thread
  while `process_job()` runs, at `lease_seconds / 3`. Renewal is conditional on
  `status = 'RUNNING' AND worker_id = :worker_id`, so a worker that *was* reaped and replaced
  cannot steal the lease back from its new owner. A lease that stops moving now means the worker
  stopped — the distinction the reaper was always trying to make.
- **Regression tests**: `tests/unit/test_worker_lease_renewal.py` — the lease advances during a
  long job, a slow job is not reaped and burns no retry, renewal stops when the job ends, and a
  genuinely abandoned lease is still recovered. The first two fail against the pre-fix code.

### 24. Extraction memory scales with document size
- **Status**: Bounded, not eliminated.
- **Context**: pdfplumber holds page objects for the document it parses. The 1,351-page PDF above
  peaked at **3.6GB RSS**. The 100MB upload cap bounds the *compressed file*, and says nothing
  about what parsing it costs — that file was 21MB.
- **Why it matters**: the Azure VM has 3.8GB of RAM in total, shared with Postgres, MinIO, the API
  and five workers. An OOM kill is **not** a `PermanentProcessingError`: the container dies, the
  lease lapses, the reaper re-queues the job, and the next worker OOMs on the same document —
  an infinite crash loop that also takes down whatever shares the host.
- **What was done**: `MAX_PDF_PAGES` moved from a module constant to `settings`, so a deployment
  can lower the ceiling without a code change (prod sets **600**); `mem_limit` added to the
  extraction and embedding containers in both compose files, so the cost is attributable and
  contained rather than host-wide. Rejecting a document with `PAGE_LIMIT_EXCEEDED` is strictly
  better than the crash loop.
- **Still open**: the relationship between page count and memory is empirical (~2.7MB/page on one
  sample), not enforced. A pathological 600-page PDF could still exceed the limit. The durable fix
  is streaming extraction page-by-page instead of holding the whole document; the trigger is the
  first `PAGE_LIMIT_EXCEEDED` rejection of a document A-PAG actually needs.

### 25. ✅ The container healthcheck could not tell a long job from a dead worker (Closed)
- **Found**: alongside #23. `apag-extraction-worker` reported `unhealthy` for the entire 139s it
  was working correctly, because the heartbeat file was only touched in the **poll loop**, which
  does not run during a single long `process_job()` call.
- **Why it mattered**: under restart-on-unhealthy, the worker doing the most expensive job in the
  queue is the one most likely to be killed — and killed repeatedly, since the job is re-queued
  each time. It also trains whoever is watching to ignore `unhealthy`.
- **Fix**: `_LeaseRenewer` touches the heartbeat on the same cycle as the lease renewal, so
  liveness is reported from inside the work rather than only between units of work.

### 26. ✅ Raw SQL bound `uuid.UUID` directly, which SQLite refuses (Closed)
- **Found**: while writing the tests for #23. `text()` carries no type information, so SQLAlchemy
  passes the value to the driver untouched — and sqlite3 rejects a `uuid.UUID` with "type 'UUID'
  is not supported". The two backends also disagree on spelling: SQLAlchemy's `Uuid` type stores
  native `uuid` on Postgres but **dash-less 32-character hex** on SQLite, so the obvious `str(id)`
  workaround matches zero rows there — and an `UPDATE` affecting zero rows raises nothing.
- **Impact**: `_mark_job_completed`, `_mark_job_failed` and the retry path were unreachable from
  the SQLite-backed unit tests. Production was unaffected (Postgres), but the paths that decide
  whether a job is retried or condemned had no unit coverage.
- **Fix**: `_job_id_param()` renders the id per dialect, so one spelling works on both and those
  paths are now exercisable in unit tests.

### 27. ✅ List and search filtered permissions in Python, after the query (Closed)
- **Found**: while designing retrieval, which must not copy the pattern. `list_documents` and
  `search_documents` fetched a page and then dropped the rows the caller could not see:
  ```python
  docs, total = repo.list_paginated(limit=limit, offset=offset)
  visible = [d for d in docs if _can_view(d, current_user)]
  ```
- **Three separate consequences**:
  1. `total` was the **unfiltered** count and was returned to the client, disclosing how many
     restricted documents exist. Verified against the live corpus: a user owning nothing saw
     `total=63` against 59 visible documents — the difference being exactly the restricted count.
  2. Invisible rows **consumed slots** in the page, so `limit=10` could return three documents
     while visible ones waited on the next page.
  3. `offset` counted rows the caller could not see, so paging forward skipped visible documents.
- **Fix**: the predicate moved into the query. `list_paginated()` and `search()` now take
  `viewer_id` and `viewer_is_admin` as **required keyword arguments with no default** — a default
  would have to mean either "see everything" (a silent leak the first time someone forgets) or
  "see nothing" (a silent empty page), so forgetting is now a `TypeError` at the call site.
- **The rule was also spelled three times** — `_can_view()`, the list comprehensions, and
  retrieval's hand-written SQL — which is how a policy change lands in one place and not the
  others. `src/modules/auth/access.py` now holds it once, in a Python form and a SQL form, and
  `tests/unit/test_access_rule.py` enumerates all 24 combinations and requires the two to agree.
  Two disagreements that the equivalence tests forced out: SQL `classification <> 'RESTRICTED'`
  evaluates to NULL for a NULL classification and would have hidden the row (fixed with
  `IS DISTINCT FROM`), and an anonymous viewer would have emitted `uploader_user_id = NULL`.
- **Regression tests**: `tests/unit/test_list_search_permissions.py` — 5 of its 9 tests fail
  against the pre-fix code, one per symptom above.
- **Still open**: chunk-level full-text search does not exist (`document_chunks` has no
  `search_vector`), so hybrid lexical+vector retrieval is not possible yet. `ts_rank` scores are
  unnormalised, so fusing them with vector scores would need Reciprocal Rank Fusion rather than
  score addition.

### 28. ✅ 2.9% of embedded passages were silently truncated at the model's window (Closed)
- **Found**: building the token readout for the search UI, which meant measuring real token
  counts for the first time.
- **Cause**: chunks were sized in **characters** (`CHUNK_MAX_CHARS = 2000`) while the model's
  window is in **tokens** (512). Those agree only at a particular ratio. This corpus averages
  4.11 chars/token — so 512 tokens is ~2100 characters and a 2000-character budget fit with 5%
  to spare — but passages containing code, terminal output or ASCII tables run ~3.15, where 512
  tokens is only ~1600 characters. 83 of 2,816 chunks (2.9%) overflowed; the worst was 1,023
  tokens, meaning **half that passage never reached the model**. fastembed truncated silently:
  the stored text was whole, the vector covered only its head, and a search for anything in the
  tail could not match.
- **The part worth remembering**: every one of the 83 was *within* the character budget, and
  every one had a proper section heading. The chunker was obeying its rules exactly. The rules
  were in a unit that could not see what they constrained. `CLAUDE.md` predicted this for
  Devanagari (2-3x more tokens per character); it arrived first in English technical prose.
- **Fix**: sizing moved behind a `SizeBudget` (`chunking/sizing.py`). `TokenBudget` measures
  through the model's own tokenizer and caps at `min(CHUNK_MAX_TOKENS, model window)`, so neither
  a generous setting nor a model swap can produce a chunk the model would truncate.
  `CharacterBudget` remains as the fallback for callers with no tokenizer. Table groups are split
  by **rows** and never by characters, because hard-wrapping a table produces exactly the
  fragment of numbers the repeated header exists to prevent.
- **Verified on the real corpus**: re-chunked and re-embedded; chunks 2,816 → 2,860 (the dense
  ones split further), truncated 83 → 0.
- **Regression tests**: `tests/unit/test_chunk_sizing.py`, including one that pins the *old*
  character budget as overflowing, so the bug cannot return unnoticed.

### 29. ✅ Batch token counting reported the longest text's length for every text (Closed)
- **Found**: within an hour of writing it, by looking at a live search response where all five
  passages claimed exactly 689 tokens.
- **Cause**: `Tokenizer.encode_batch` pads every sequence out to the longest in the batch, and the
  shipped tokenizer has padding configured. Disabling truncation was not enough; padding inflates
  short texts instead of capping long ones.
- **Why it would have survived review**: the numbers were plausible in isolation and only obviously
  wrong side by side. It also inflated `context_tokens` (3,445 against a true 1,811) and produced
  false truncation warnings — a measurement built to reveal a silent failure, itself failing
  silently.
- **Fix**: `no_padding()` alongside `no_truncation()` on the counting tokenizer. Two regression
  tests in `tests/unit/test_embedding.py` fail against the pre-fix code.

### 30. Retrieval quality is unmeasured — there is no evaluation set
- **Status**: Open, and the blocker for every tuning decision that follows.
- **Context**: hybrid search ships with several constants chosen from the literature and from
  inspection, not from measurement on this corpus: RRF's `k=60` (Cormack et al. 2009), the
  candidate pool multiplier of 5, the 'A'/'B' weighting of body against section heading, and the
  choice of `english` as the text-search configuration.
- **Why they are not tunable yet**: tuning requires knowing whether a change helped, which
  requires a set of questions with known-correct passages. There is none. Without it, adjusting
  `k` is guessing, and the honest thing is to leave a defensible published default in place.
- **What exists instead**: the `mode` parameter, which lets a disappointing result be attributed
  to an arm — "the lexical arm found it and the vector arm did not" is a real diagnosis, and
  `semantic_rank`/`lexical_rank` on every result make it visible per passage. Measured ad hoc on
  16 queries, the lexical arm added results the vector arm missed on 6 of them.
- **Trigger to address**: before tuning anything, and before choosing between embedding models on
  anything other than cost. Thirty to fifty real A-PAG questions with the passages that answer
  them would be enough to compute recall@k and MRR, and would turn every constant above from a
  guess into a decision. That list has to come from people who know the corpus, not from me.

### 31. ✅ Integration tests built their schema from the ORM, so no migration ever ran (Closed)
- **Found**: writing the first test of the lexical arm, which returned nothing. The query was
  correct; the trigger that populates `search_vector` had simply never been created.
- **Cause**: `tests/integration/conftest.py` built the schema with `Base.metadata.create_all()`,
  which knows only what the ORM declares. Triggers, functions and several constraints live in
  migrations, so the test database was missing the `audit_log` immutability triggers (`0003`),
  the partial unique index that is the real dedup guarantee (`0004`), and both tsvector triggers
  (`0006`, `0015`).
- **Why it mattered beyond this feature**: any test of DB-enforced behaviour was passing or
  failing for the wrong reason, and a test asserting that `audit_log` cannot be updated would
  have passed against a database with no such trigger.
- **Fix**: the suite now runs `alembic upgrade head` against the throwaway container, so the test
  schema is the schema that ships. `env.py` was changed to respect a caller-supplied
  `sqlalchemy.url` instead of unconditionally overriding it with `settings.DATABASE_URL` — which
  would have pointed the suite's migrations at the developer's real database, exactly what the
  existing `APAG_ALLOW_DESTRUCTIVE_DB_TESTS` guard exists to prevent.

### 32. The chunking worker loads the embedding model purely to tokenize
- **Status**: Accepted trade-off, with a cheaper option deliberately not taken.
- **Context**: sizing chunks in tokens requires the model's tokenizer, and the only robust way to
  obtain it is `FastEmbedProvider`, which loads the ONNX model. The chunking worker therefore
  carries ~640MB resident (observed 524MB) for a stage that performs no inference.
- **The cheaper option**: the tokenizer itself is a 695KB `tokenizer.json` inside fastembed's
  cache. Loading it directly would cost a few MB. It was **not** taken because finding it means
  hardcoding fastembed's cache layout (`models--Qdrant--bge-base-en-v1.5-onnx-Q/snapshots/<hash>/`),
  which is undocumented and would break silently on a fastembed upgrade — and the failure mode
  is a fallback to the character budget, i.e. a quiet return of debt #28.
- **What contains it**: `mem_limit` on the chunking container (2g dev, 1200m prod) so the cost is
  attributable and bounded rather than host-wide, and `ChunkingService.with_model_tokenizer()`
  falls back to the character budget with a WARNING rather than refusing to start.
- **Trigger to address**: if the VM proves too small for two model-loading workers, or if
  fastembed exposes a supported way to fetch a tokenizer without the model. Either makes this a
  small, local change.

### 33. `AWAITING_CLASSIFICATION` no longer means what it says
- **Status**: Open. A rename, not a design flaw.
- **Context**: the status was named when a human classification gate was planned. That gate was
  dropped — the uploader picks the sensitivity tier at upload — and the status was repurposed to
  mean "normalized, ready to chunk". Nothing classifies anything at that point.
- **Why it costs something**: a name that states the wrong thing is worse than an opaque one,
  because a reader trusts it. It has already cost review time more than once, and anyone new to
  the pipeline reasonably assumes a document sitting there is waiting on a person.
- **Why not yet**: it spans `DocumentStatus`, a CHECK-constraint migration, five handlers, the
  backfill script's `STAGE_ENTRY_STATUS` map, both UIs and a number of tests, and it touches
  documents currently in that state. Worth one deliberate change rather than a drive-by.
- **Trigger to address**: the next migration that touches `chk_documents_status` anyway —
  `NORMALIZED` is the honest name.

### 34. ✅ `/documents/test-preset/{name}` served fixtures without authentication (Closed)
- **Found**: a documentation review challenged the claim that all of `/documents/*` requires a
  bearer token. It did not: this one route had no `current_user` dependency.
- **Impact**: anyone who could reach the API could download the validation fixtures, including
  `disguised_malware.pdf` and `threat_exploit_sample.pdf`. They are crafted to trip the validator
  rather than to do harm, and `preset_name` is matched against a fixed map so there was no path
  traversal — but an unauthenticated endpoint handing out files named malware does not belong on
  a box with a public IP, and it made a documented security claim false.
- **Fix**: the route now depends on `get_current_user` like every other. The Studio UI already had
  an `authHeaders()` helper and simply was not using it for this one call.
