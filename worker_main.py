"""Worker Process Entrypoint for A-PAG AI Knowledge Base.

One process runs one stage, selected by WORKER_STAGE (default SCAN, so an existing deployment
that sets nothing keeps behaving exactly as before). Each compose service runs this same image
with a different WORKER_STAGE — the container healthcheck model assumes one worker daemon per
container (see KNOWN_DEBTS.md #5), so stages get their own containers rather than threads.
"""

import logging
import os
import sys
from typing import Protocol

from src.workers.chunking_worker import ChunkingWorker
from src.workers.drive_sync_worker import DriveSyncWorker
from src.workers.embedding_worker import EmbeddingWorker
from src.workers.extraction_worker import ExtractionWorker
from src.workers.normalization_worker import NormalizationWorker
from src.workers.scan_worker import ScanWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("worker_main")

class Worker(Protocol):
    """Anything with a blocking `run()`. The pipeline stages are `BaseWorker`s that claim rows
    from the jobs table; DRIVE_SYNC runs on a clock instead, so the contract is just this."""

    def run(self) -> None: ...


WORKERS: dict[str, type[Worker]] = {
    "SCAN": ScanWorker,
    "EXTRACT": ExtractionWorker,
    "NORMALIZE": NormalizationWorker,
    "CHUNK": ChunkingWorker,
    "EMBED": EmbeddingWorker,
    # Not a pipeline stage and not in JobStage: nothing enqueues it. See drive_sync_worker.py.
    "DRIVE_SYNC": DriveSyncWorker,
}


def build_worker(stage: str) -> Worker:
    worker_cls = WORKERS.get(stage)
    if worker_cls is None:
        raise SystemExit(
            f"Unknown WORKER_STAGE '{stage}'. Supported stages: {', '.join(sorted(WORKERS))}."
        )
    return worker_cls()


def main() -> None:
    stage = os.environ.get("WORKER_STAGE", "SCAN").strip().upper()
    logger.info("Initializing A-PAG %s worker process...", stage)
    build_worker(stage).run()


if __name__ == "__main__":
    main()
