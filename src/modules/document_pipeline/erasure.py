"""Deleting a document deletes everything of it, at once.

A delete used to be reversible by default: the document left search, and its bytes, text and
passages stayed so the delete could be undone. In use that read as broken -- the file was gone
from the list, yet uploading it again was refused as "already there", because the hidden copy
still matched. So a delete now erases: the stored file, the text read out of it, its passages and
their vectors, the refused re-uploads of it, and its unfinished jobs. Uploading the same file
afterwards adds it as a new document.

One thing is kept on purpose: the audit log's record that it was deleted, by whom and when. That
log is append-only at the database level (migration 0003) and holds no document content -- an
erasure that also erased the record of itself could not be shown to have happened.
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.db.enums import JobStatus
from src.db.models import DocumentChunk as ChunkORM
from src.db.models import Job as JobORM
from src.modules.document_pipeline.repository import DocumentRepository
from src.modules.document_pipeline.storage_keys import (
    extraction_key_for,
    normalized_key_for,
)

logger = logging.getLogger(__name__)


class DocumentBusy(Exception):
    """A worker is processing the document right now. Erasing under it would let it write the
    passages back a moment later, so the caller is asked to try again."""


class StorageEraseFailed(Exception):
    """An object could not be removed from storage. Nothing about the document has been
    tombstoned, so the delete can be retried."""


def erase_document(
    db: Session,
    repo: DocumentRepository,
    buckets,
    doc,
    record: Callable[[], None] = lambda: None,
) -> None:
    """Erases a document and everything derived from it.

    Order matters. Its unfinished jobs are claimed first, so no worker can pick one up while the
    rest happens; `record` (the audit entry) runs only once the delete is known to go ahead; the
    stored objects go before the row is tombstoned, so a storage failure leaves the document
    exactly as it was and the delete can be retried.
    """
    jobs = db.execute(
        select(JobORM)
        .where(
            JobORM.document_id == doc.id,
            JobORM.status.in_([JobStatus.PENDING.value, JobStatus.RUNNING.value]),
        )
        .with_for_update()
    ).scalars().all()
    if any(job.status == JobStatus.RUNNING.value for job in jobs):
        db.rollback()
        raise DocumentBusy(str(doc.id))
    now = datetime.now(UTC)
    for job in jobs:
        job.status = JobStatus.FAILED.value
        job.finished_at = now
        job.error_details = {"reason": "document deleted"}
    db.flush()

    record()

    objects = [
        (buckets.raw, doc.raw_path),
        (buckets.quarantine, doc.quarantine_path),
        (buckets.extracted, extraction_key_for(doc.id)),
        (buckets.normalized, normalized_key_for(doc.id)),
    ]
    for bucket, path in objects:
        if not path:
            continue
        key = path.split("/", 1)[-1] if path.startswith(f"{bucket}/") else path
        if buckets.storage.object_exists(bucket, key) and not buckets.storage.delete_object(
            bucket_name=bucket, object_name=key,
        ):
            db.rollback()
            raise StorageEraseFailed(f"{bucket}/{key}")

    removed = db.execute(delete(ChunkORM).where(ChunkORM.document_id == doc.id)).rowcount
    db.commit()

    checksum = doc.checksum
    repo.purge(doc.id)
    duplicates = repo.purge_duplicates_of(checksum) if checksum else 0
    logger.info("ERASED: doc_id=%s passages=%s refused_reuploads=%s", doc.id, removed, duplicates)
