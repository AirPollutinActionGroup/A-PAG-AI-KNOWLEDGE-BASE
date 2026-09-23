"""Chunking service — the seam the job handler talks to."""

from src.modules.document_pipeline.chunking.chunker import Chunker
from src.modules.document_pipeline.chunking.models import ChunkingResult
from src.modules.document_pipeline.normalization.models import NormalizationResult


class ChunkingService:
    """Turns a normalized document into retrievable passages.

    Pure: no storage, no database, no awareness of job state. That is what lets the chunking
    rules be tested against a hand-built NormalizationResult with no stack around them.
    """

    def __init__(self, chunker: Chunker | None = None):
        self.chunker = chunker or Chunker()

    def chunk(self, normalized: NormalizationResult) -> ChunkingResult:
        return ChunkingResult(
            document_id=normalized.document_id,
            chunks=self.chunker.chunk(normalized),
        )
