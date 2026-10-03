"""Enqueue pipeline jobs for documents that never got one.

Each stage hands off to the next by inserting a job row, which means a document that came to rest
*before* a stage existed has no job for it and never will. There are documents sitting at
AWAITING_CLASSIFICATION that predate chunking, and the same happens at every stage boundary the
first time it ships. This is also how a bulk archive import gets moving after the documents are
already in the database.

Idempotent: a document that already has a PENDING or RUNNING job for the stage is skipped, so
running this twice cannot double-queue work.

    python backfill_jobs.py --stage CHUNK  --status AWAITING_CLASSIFICATION --dry-run
    python backfill_jobs.py --stage CHUNK  --status AWAITING_CLASSIFICATION
    python backfill_jobs.py --stage EMBED  --status CHUNKED

`--reset` additionally moves the selected documents back to the stage's entry status before
queueing. That is the case where a *stage improved* rather than a document being stranded: the
OCR fallback made three scanned PDFs readable that had come to rest at NORMALIZATION_FAILED, and
without a reset the handler's idempotency guard correctly refuses to touch them. Expect to need
it again whenever an extractor, a chunking rule or an embedding model changes.

    python backfill_jobs.py --stage EXTRACT --status NORMALIZATION_FAILED --reset --dry-run
    python backfill_jobs.py --stage EXTRACT --status NORMALIZATION_FAILED --reset
"""

import argparse
import logging
import sys
import uuid

from sqlalchemy import select

from src.db.engine import SessionLocal
from src.db.enums import DocumentStatus, JobStage, JobStatus
from src.db.models import Document as DocumentORM
from src.db.models import Job as JobORM

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("backfill")

# The status a document must be in for each stage to accept it. Mirrors the idempotency guard at
# the top of every job handler's process() — queueing a stage for a document in the wrong state
# just produces a no-op job, which wastes a worker cycle and muddies the queue.
STAGE_ENTRY_STATUS = {
    JobStage.SCAN: DocumentStatus.QUARANTINED,
    JobStage.EXTRACT: DocumentStatus.VALIDATED,
    JobStage.NORMALIZE: DocumentStatus.EXTRACTED,
    JobStage.CHUNK: DocumentStatus.AWAITING_CLASSIFICATION,
    JobStage.EMBED: DocumentStatus.CHUNKED,
}


def backfill(stage: JobStage, status: DocumentStatus, dry_run: bool, limit: int | None,
             reset: bool = False) -> int:
    with SessionLocal() as db:
        # Documents in the target state with no live job for this stage. COMPLETED and FAILED
        # jobs are deliberately not counted as live: a document still sitting in the entry state
        # with a finished job is one whose job did not take effect, and it should be re-queued.
        live_job = (
            select(JobORM.document_id)
            .where(
                JobORM.stage == stage.value,
                JobORM.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
            )
        )
        query = (
            select(DocumentORM)
            .where(
                DocumentORM.status == status.value,
                DocumentORM.deleted_at.is_(None),
                DocumentORM.document_id.not_in(live_job),
            )
            .order_by(DocumentORM.created_at)
        )
        if limit:
            query = query.limit(limit)

        docs = db.execute(query).scalars().all()

        if not docs:
            logger.info("Nothing to do: no %s documents without a live %s job.",
                        status.value, stage.value)
            return 0

        logger.info("Found %d document(s) in %s with no live %s job.",
                    len(docs), status.value, stage.value)
        for doc in docs:
            logger.info("  %s  %s", doc.document_id, doc.filename)

        if reset:
            entry = STAGE_ENTRY_STATUS[stage]
            logger.info("--reset: %d document(s) will be moved %s -> %s before queueing.",
                        len(docs), status.value, entry.value)

        if dry_run:
            logger.info("Dry run — nothing was queued. Re-run without --dry-run to enqueue.")
            return 0

        if reset:
            entry = STAGE_ENTRY_STATUS[stage]
            for doc in docs:
                doc.status = entry.value
                # Cleared deliberately: it described the previous failure, and leaving it on a
                # document that is about to be re-read would outlive the thing it explained.
                doc.rejection_reason = None

        for doc in docs:
            db.add(JobORM(
                job_id=uuid.uuid4(),
                document_id=doc.document_id,
                stage=stage.value,
                status=JobStatus.PENDING.value,
            ))
        db.commit()
        logger.info("Enqueued %d %s job(s).", len(docs), stage.value)
        return len(docs)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True, choices=[s.value for s in JobStage])
    parser.add_argument(
        "--status",
        help="Document status to select. Defaults to the stage's normal entry status.",
    )
    parser.add_argument("--dry-run", action="store_true", help="List documents without queueing.")
    parser.add_argument("--limit", type=int, help="Cap how many are queued in one run.")
    parser.add_argument(
        "--reset", action="store_true",
        help="Move the documents back to the stage's entry status first. For re-running a "
             "stage that has since improved.",
    )
    args = parser.parse_args()

    stage = JobStage(args.stage)
    status = DocumentStatus(args.status) if args.status else STAGE_ENTRY_STATUS[stage]

    if STAGE_ENTRY_STATUS[stage] != status and not args.reset:
        # Allowed, because re-running a stage over documents in another state is occasionally
        # what you want — but it is not the normal path, and the handler's guard will no-op most
        # of them, so say so rather than let it look like it worked.
        logger.warning(
            "Stage %s normally consumes %s documents, not %s. Most will be skipped by the "
            "handler's idempotency guard.",
            stage.value, STAGE_ENTRY_STATUS[stage].value, status.value,
        )

    backfill(stage, status, args.dry_run, args.limit, reset=args.reset)


if __name__ == "__main__":
    sys.exit(main())
