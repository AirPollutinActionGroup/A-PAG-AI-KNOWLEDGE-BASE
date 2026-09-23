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
  `COMPLETED` rows from `jobs`. Every document now produces four of them (SCAN, EXTRACT,
  NORMALIZE, CHUNK), so the table grows at four rows per document forever.
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
