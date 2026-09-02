"""Worker Process Entrypoint for A-PAG AI Knowledge Base."""

import logging
import sys

from src.core.config import settings
from src.workers.scan_worker import ScanWorker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("worker_main")


def main() -> None:
    logger.info("Initializing A-PAG Scan Worker process...")
    worker = ScanWorker()
    worker.run()


if __name__ == "__main__":
    main()
