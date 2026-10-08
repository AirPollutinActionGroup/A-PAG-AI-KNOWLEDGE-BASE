"""Ingest a folder of documents in one go.

The HTTP upload endpoint is built for a person with a handful of files: it caps a request at
`MAX_FILES_PER_UPLOAD`, rate-limits per client, and holds every file of a batch in memory to
answer within one request. None of that suits an archive. This walks a directory instead and
calls `UploadService.receive()` directly — the same entry point the endpoint uses, so a document
ingested here is indistinguishable afterwards from one uploaded by hand.

What it deliberately does *not* do is process anything. It quarantines each file and queues a
SCAN job, exactly as the API does; the workers pick them up at their own pace. That is what keeps
a 5GB import from being one enormous transaction that fails as a unit — each document succeeds or
fails alone, and re-running skips what is already in.

    python bulk_ingest.py ./archive --owner someone@a-pag.org --dry-run
    python bulk_ingest.py ./archive --owner someone@a-pag.org
    python bulk_ingest.py ./archive --owner someone@a-pag.org --classification RESTRICTED

Re-running is safe. A file whose bytes are already in the corpus is promoted to DUPLICATE by the
scan stage rather than stored twice, so an interrupted import is resumed by running it again.
"""

import argparse
import logging
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select

from src.core.config import settings
from src.db.engine import SessionLocal
from src.db.enums import Classification, DocumentStatus
from src.db.models import Document as DocumentORM
from src.db.models import User as UserORM
from src.modules.document_pipeline.formats import FORMATS, detect_format
from src.modules.document_pipeline.models import UploadRequest
from src.modules.document_pipeline.repository import PostgreSQLDocumentRepository
from src.modules.document_pipeline.upload_service import UploadService
from src.storage.bucket_manager import BucketManager

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger("bulk_ingest")
logger.setLevel(logging.INFO)

# Extensions worth *attempting*. The real decision is made from file content by detect_format();
# this only avoids reading every .zip and .mp4 in a directory tree to discover it isn't a
# document. Derived from the format registry so adding a format does not need editing here.
CANDIDATE_SUFFIXES = {spec.extension.lower() for spec in FORMATS.values()}


@dataclass
class Tally:
    queued: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    unsupported: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped_empty: list[str] = field(default_factory=list)

    @property
    def seen(self) -> int:
        return (len(self.queued) + len(self.duplicates) + len(self.unsupported)
                + len(self.failed) + len(self.skipped_empty))


def resolve_owner(email: str) -> uuid.UUID:
    """Every document needs an uploader: `documents.uploader_user_id` carries a real FK, and the
    RESTRICTED tier is owner-scoped, so a document with no owner is one nobody but an admin can
    ever see."""
    with SessionLocal() as db:
        user = db.execute(select(UserORM).where(UserORM.email == email)).scalar_one_or_none()
        if user is None:
            raise SystemExit(
                f"No user with email {email!r}. Register one first "
                f"(POST /api/v1/auth/register) — documents ingested without an owner are "
                f"invisible to everyone except admins."
            )
        return user.user_id


def preflight_storage(buckets: BucketManager) -> None:
    """Refuse to run if this process writes to different storage than the workers read from.

    This is the trap that motivated the check. `.env` had `STORAGE_BACKEND=local`, so running the
    script on the host wrote every file to `./storage_data/` — while the workers, inside Docker,
    looked in MinIO. The ingest reported 39 successes and the pipeline then failed all 39 with
    NoSuchKey, several minutes later and in a different log.

    The database is what makes the check possible: if it holds documents the workers already
    promoted to `raw/`, those objects must be readable through correctly-configured storage. Not
    being able to read one means this process is pointed somewhere else.
    """
    logger.info(
        "Storage backend: %s%s",
        settings.STORAGE_BACKEND,
        f" ({settings.MINIO_ENDPOINT})" if settings.STORAGE_BACKEND == "minio" else
        " (./storage_data/)",
    )

    with SessionLocal() as db:
        probe = db.execute(
            select(DocumentORM.filename, DocumentORM.raw_path)
            .where(DocumentORM.status == DocumentStatus.LIVE.value,
                   DocumentORM.raw_path.is_not(None))
            .limit(1)
        ).first()

    if probe is None:
        # An empty corpus gives nothing to check against. Say so rather than imply a pass.
        logger.warning(
            "No promoted document to verify storage against (empty corpus?). If the workers run "
            "in Docker and this does not, STORAGE_BACKEND must match theirs — otherwise these "
            "files land somewhere the workers cannot read and every document fails at SCAN."
        )
        return

    key = probe.raw_path.split("/", 1)[-1]
    try:
        buckets.storage.get_object(buckets.raw, key)
    except Exception as e:
        raise SystemExit(
            "\n".join([
                (
                    f"Storage mismatch. This process is configured for STORAGE_BACKEND="
                    f"{settings.STORAGE_BACKEND!r}, but cannot read {probe.filename!r}, which "
                    f"the pipeline already promoted to raw storage ({e})."
                ),
                "",
                (
                    "Files ingested now would be written where the workers cannot find them, "
                    "and every one would fail at SCAN with NoSuchKey."
                ),
                "",
                "If the workers run in Docker, run this with the same backend they use:",
                "  STORAGE_BACKEND=minio MINIO_ENDPOINT=localhost:9000 python bulk_ingest.py …",
            ])
        ) from e

    logger.info("Storage verified: the pipeline's existing objects are readable from here.")


def discover(root: Path) -> list[Path]:
    if root.is_file():
        return [root]
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in CANDIDATE_SUFFIXES
        # Office writes lock files alongside open documents; they are valid ZIPs and would
        # otherwise be ingested as mangled copies of whatever is open on someone's desktop.
        and not p.name.startswith("~$")
    )


def ingest_one(service: UploadService, path: Path, meta: UploadRequest, tally: Tally) -> None:
    rel = str(path)
    try:
        data = path.read_bytes()
    except OSError as e:
        tally.failed.append((rel, f"unreadable: {e}"))
        return

    if not data:
        tally.skipped_empty.append(rel)
        return

    # Content, not extension — the same rule the API boundary uses. A .docx that is really a
    # spreadsheet is stored as the spreadsheet it is.
    spec = detect_format(data)
    if spec is None:
        tally.unsupported.append((rel, "not a PDF/DOCX/XLSX/PPTX"))
        return

    try:
        response = service.receive(
            filename=path.name,
            data=data,
            request_meta=meta,
            mime_type=spec.mime_type,
        )
    except Exception as e:
        # One bad file must not end the run. An archive of a few thousand documents will contain
        # something surprising, and finding out at file 1,900 that nothing since file 12 was
        # ingested is the failure mode worth designing against.
        logger.warning("FAILED %s: %s", rel, e)
        tally.failed.append((rel, str(e)))
        return

    if response.was_duplicate:
        tally.duplicates.append(rel)
    else:
        tally.queued.append(rel)


def report(tally: Tally, batch_id: uuid.UUID | None, dry_run: bool) -> None:
    print()
    print("=" * 72)
    print(f"{'Found' if dry_run else 'Ingested'}: {tally.seen} file(s)")
    print("-" * 72)
    print(f"  queued for processing : {len(tally.queued)}")
    print(f"  already in the corpus : {len(tally.duplicates)}")
    print(f"  unsupported format    : {len(tally.unsupported)}")
    print(f"  empty, skipped        : {len(tally.skipped_empty)}")
    print(f"  failed                : {len(tally.failed)}")

    for label, rows in (("UNSUPPORTED", tally.unsupported), ("FAILED", tally.failed)):
        if rows:
            print(f"\n{label}:")
            for name, why in rows[:20]:
                print(f"  {name}\n      {why}")
            if len(rows) > 20:
                print(f"  ... and {len(rows) - 20} more")

    if dry_run:
        print("\nDry run — nothing was ingested. Re-run without --dry-run.")
        return

    if batch_id:
        print(f"\nBatch id: {batch_id}")
        print("Track it with:")
        print("  SELECT status, count(*) FROM documents "
              f"WHERE upload_batch_id = '{batch_id}' GROUP BY 1;")
        # Not knowable from here. `receive()` only quarantines; whether a file was a duplicate,
        # and whether that duplicate raised a held document's tier, is decided by the scan worker
        # after this command has returned. Pointing at where the answer will be beats printing a
        # count that reads as one.
        print("\nDuplicates are decided by the scan stage after this returns and show above as")
        print("DUPLICATE. Any that were filed more restricted than the copy already held raised")
        print("that document to RESTRICTED; list them with:")
        print("  SELECT document_id, details->>'old_tier', details->>'new_tier', event_time")
        print("  FROM audit_log WHERE event_type = 'DOCUMENT_RECLASSIFIED'")
        print("    AND details->>'reason' = 'dedup-escalation' ORDER BY event_time DESC;")
    print("\nThe workers process these in the background. Documents become searchable as they")
    print("reach LIVE; nothing further is required here.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path, help="A file, or a directory to walk recursively.")
    parser.add_argument("--owner", required=True,
                        help="Email of the user who will own these documents.")
    # Required, not defaulted. The whole point of this script is ingesting a folder nobody
    # will review file by file, which is exactly when an unstated PUBLIC is most dangerous:
    # the gateway reads this field to decide what may be sent to an external model.
    parser.add_argument("--classification", required=True,
                        choices=[c.value for c in Classification],
                        help="Sensitivity tier for every document in this run (default PUBLIC).")
    parser.add_argument("--dry-run", action="store_true",
                        help="List what would be ingested, touching nothing.")
    parser.add_argument("--limit", type=int, help="Stop after this many files.")
    args = parser.parse_args()

    if not args.path.exists():
        raise SystemExit(f"No such path: {args.path}")

    paths = discover(args.path)
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print(f"No candidate documents under {args.path} "
              f"(looking for {', '.join(sorted(CANDIDATE_SUFFIXES))}).")
        return 0

    owner_id = resolve_owner(args.owner)
    tally = Tally()

    if args.dry_run:
        preflight_storage(BucketManager())
        for p in paths:
            print(f"  {p}")
        tally.queued = [str(p) for p in paths]
        report(tally, None, dry_run=True)
        return 0

    # One batch id across the run, so the whole import can be found, counted and — if it was a
    # mistake — acted on as a unit afterwards.
    batch_id = uuid.uuid4()
    meta = UploadRequest(
        classification=Classification(args.classification),
        owner_id=owner_id,
        upload_batch_id=batch_id,
    )

    total = len(paths)
    print(f"Ingesting {total} file(s) as {args.owner} [{args.classification}], batch {batch_id}\n")

    # A session and service per file. The alternative — one long-lived session — holds a
    # transaction open for the length of the import, so a failure late in a large run can roll
    # back work that had already succeeded.
    buckets = BucketManager()
    preflight_storage(buckets)

    for i, path in enumerate(paths, 1):
        with SessionLocal() as db:
            service = UploadService(
                bucket_manager=buckets,
                repository=PostgreSQLDocumentRepository(db),
                db_session=db,
            )
            ingest_one(service, path, meta, tally)
        print(f"\r  [{i}/{total}] {path.name[:56]:<58}", end="", flush=True)

    report(tally, batch_id, dry_run=False)
    return 1 if tally.failed else 0


if __name__ == "__main__":
    sys.exit(main())
