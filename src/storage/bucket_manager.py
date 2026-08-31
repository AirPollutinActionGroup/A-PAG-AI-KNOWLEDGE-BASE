"""Bucket Manager for 2-bucket architecture: apag-quarantine and apag-raw."""

import logging

from src.storage.object_storage import (
    LocalFileSystemStorage,
    MinIOStorage,
    ObjectStorage,
)

logger = logging.getLogger(__name__)


class BucketManager:
    """Manages quarantine and raw storage buckets."""

    QUARANTINE_BUCKET = "apag-quarantine"
    RAW_BUCKET = "apag-raw"

    def __init__(
        self,
        storage: ObjectStorage | None = None,
        use_local: bool = True,
        local_dir: str = "./storage_data",
        endpoint: str = "localhost:9000",
        access_key: str = "apag_admin",
        secret_key: str = "apag_secure_password_2026",
        secure: bool = False,
    ):
        if storage:
            self.storage = storage
        elif use_local:
            self.storage = LocalFileSystemStorage(base_dir=local_dir)
            logger.info("Storage backend: LocalFileSystem (%s)", local_dir)
        else:
            try:
                self.storage = MinIOStorage(
                    endpoint=endpoint,
                    access_key=access_key,
                    secret_key=secret_key,
                    secure=secure,
                )
                logger.info("Storage backend: MinIO (%s)", endpoint)
            except Exception:
                logger.warning(
                    "MinIO connection failed (endpoint=%s). "
                    "Falling back to local filesystem at '%s'. "
                    "THIS IS NOT PRODUCTION-SAFE.",
                    endpoint, local_dir,
                )
                self.storage = LocalFileSystemStorage(base_dir=local_dir)

        # Ensure both buckets exist
        self.storage.ensure_bucket_exists(self.QUARANTINE_BUCKET)
        self.storage.ensure_bucket_exists(self.RAW_BUCKET)
        logger.info("Buckets ready: [%s, %s]", self.QUARANTINE_BUCKET, self.RAW_BUCKET)

    @property
    def quarantine(self) -> str:
        return self.QUARANTINE_BUCKET

    @property
    def raw(self) -> str:
        return self.RAW_BUCKET
