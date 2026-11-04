"""Dry run of an extraction change against the real corpus. Reads, never writes.

Before re-processing documents with a changed extractor, cleaner or chunker, run the new code over
each document in memory and compare it with what is stored. The thing to stop on is a **flip**:
a document whose effective language changes (a spreadsheet newly read as Croatian is skipped at
embedding and drops out of meaning-based search) or whose quality verdict changes. Passage counts
are reported so the size of the change is known before it happens.

    python compare_extraction.py --mime xlsx --mime pptx --mime docx
    python compare_extraction.py --method OCR --skip-larger-than 20
    python compare_extraction.py --document-id <uuid> --show
"""

import argparse
import sys
import uuid

from sqlalchemy import func, select

from src.core.config import settings
from src.db.engine import SessionLocal
from src.db.models import Document as DocumentORM
from src.db.models import DocumentChunk as ChunkORM
from src.modules.document_pipeline.chunking.chunker import Chunker
from src.modules.document_pipeline.embedding_job_handler import prose_ratio
from src.modules.document_pipeline.extraction.service import ExtractionService
from src.modules.document_pipeline.formats import (
    DOCX_MIME,
    PDF_MIME,
    PPTX_MIME,
    XLSX_MIME,
)
from src.modules.document_pipeline.normalization.models import NormalizationResult
from src.modules.document_pipeline.normalization.service import NormalizationService
from src.modules.document_pipeline.storage_keys import normalized_key_for
from src.storage.bucket_manager import BucketManager

MIME_BY_NAME = {"pdf": PDF_MIME, "docx": DOCX_MIME, "xlsx": XLSX_MIME, "pptx": PPTX_MIME}


def effective_language(result: NormalizationResult) -> str | None:
    """What the embedding stage will act on -- the same rule as
    `EmbeddingJobHandler._detected_language`."""
    if prose_ratio(result.full_text) < settings.EMBEDDING_MIN_PROSE_RATIO:
        return "en"
    return result.language


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mime", action="append", choices=sorted(MIME_BY_NAME))
    parser.add_argument("--method", choices=["NATIVE", "OCR", "MIXED"])
    parser.add_argument("--document-id", action="append", type=uuid.UUID)
    parser.add_argument("--skip-larger-than", type=int, metavar="UNITS",
                        help="Skip documents with more pages/slides/sheets than this.")
    parser.add_argument("--show", action="store_true", help="Print the new chunks.")
    args = parser.parse_args()

    buckets = BucketManager()
    extraction, normalization, chunker = ExtractionService(), NormalizationService(), Chunker()
    flips = 0

    with SessionLocal() as db:
        query = select(DocumentORM).where(
            DocumentORM.deleted_at.is_(None), DocumentORM.status == "LIVE",
        )
        if args.mime:
            query = query.where(DocumentORM.mime_type.in_([MIME_BY_NAME[m] for m in args.mime]))
        if args.method:
            query = query.where(DocumentORM.extraction_method == args.method)
        if args.document_id:
            query = query.where(DocumentORM.document_id.in_(args.document_id))
        docs = db.execute(query.order_by(DocumentORM.mime_type, DocumentORM.filename)).scalars().all()

        print(f"{'document':48} {'lang old->new':14} {'gate':9} {'passages old->new':18} notes")
        for doc in docs:
            if args.skip_larger_than and (doc.page_count or 0) > args.skip_larger_than:
                print(f"{doc.filename.rsplit('/', 1)[-1][:48]:48} skipped ({doc.page_count} units)")
                continue
            old = NormalizationResult.model_validate_json(
                buckets.storage.get_object(buckets.normalized, normalized_key_for(doc.document_id))
            )
            data = buckets.storage.get_object(buckets.raw, doc.raw_path.split("/", 1)[-1])
            extracted = extraction.extract(doc.document_id, data, doc.mime_type)
            new = normalization.normalize(extracted)
            chunks = chunker.chunk(new)
            old_count = db.execute(
                select(func.count()).select_from(ChunkORM)
                .where(ChunkORM.document_id == doc.document_id)
            ).scalar_one()

            lang_old, lang_new = effective_language(old), effective_language(new)
            notes = []
            if lang_old != lang_new:
                notes.append("LANGUAGE FLIP")
            if old.quality.passed != new.quality.passed:
                notes.append("GATE FLIP")
            if extracted.ocr_unreadable_lines:
                notes.append(f"unreadable OCR lines {extracted.ocr_unreadable_lines}")
            flips += bool(notes and ("FLIP" in " ".join(notes)))
            gate = "pass" if new.quality.passed else "FAIL"
            print(f"{doc.filename.rsplit('/', 1)[-1][:48]:48} {f'{lang_old}->{lang_new}':14} "
                  f"{gate:9} {f'{old_count}->{len(chunks)}':18} {'; '.join(notes)}")
            if args.show:
                for c in chunks:
                    print(f"    [{c.page_number}] {c.section_heading!r} table={c.is_table}: "
                          f"{c.text[:160]!r}")

    print(f"\n{flips} document(s) would flip language or quality verdict.")
    return 1 if flips else 0


if __name__ == "__main__":
    sys.exit(main())
