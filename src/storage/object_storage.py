"""Object storage abstraction and implementations for quarantine and raw storage."""

from __future__ import annotations

import io
import os
import shutil
from abc import ABC, abstractmethod


class ObjectStorage(ABC):
    """Abstract object storage contract."""

    @abstractmethod
    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: bytes,
        content_type: str = "application/pdf",
    ) -> str:
        """Stores object bytes into the bucket and returns the object key."""

    @abstractmethod
    def get_object(self, bucket_name: str, object_name: str) -> bytes:
        """Retrieves raw bytes of an object."""

    @abstractmethod
    def delete_object(self, bucket_name: str, object_name: str) -> bool:
        """Deletes an object from the bucket."""

    @abstractmethod
    def object_exists(self, bucket_name: str, object_name: str) -> bool:
        """Checks if an object exists in the bucket."""

    @abstractmethod
    def copy_object(
        self,
        source_bucket: str,
        source_object: str,
        dest_bucket: str,
        dest_object: str,
    ) -> str:
        """Copies an object from source bucket to destination bucket."""

    @abstractmethod
    def ensure_bucket_exists(self, bucket_name: str) -> None:
        """Ensures that the target bucket/directory is created."""


class LocalFileSystemStorage(ObjectStorage):
    """Local filesystem-backed storage implementation for development and testing."""

    def __init__(self, base_dir: str = "./storage_data"):
        self.base_dir = os.path.abspath(base_dir)
        os.makedirs(self.base_dir, exist_ok=True)

    def _get_full_path(self, bucket_name: str, object_name: str) -> str:
        return os.path.join(self.base_dir, bucket_name, object_name)

    def ensure_bucket_exists(self, bucket_name: str) -> None:
        bucket_path = os.path.join(self.base_dir, bucket_name)
        os.makedirs(bucket_path, exist_ok=True)

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: bytes,
        content_type: str = "application/pdf",
    ) -> str:
        self.ensure_bucket_exists(bucket_name)
        full_path = self._get_full_path(bucket_name, object_name)
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(data)
        return object_name

    def get_object(self, bucket_name: str, object_name: str) -> bytes:
        full_path = self._get_full_path(bucket_name, object_name)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Object '{object_name}' not found in bucket '{bucket_name}'")
        with open(full_path, "rb") as f:
            return f.read()

    def delete_object(self, bucket_name: str, object_name: str) -> bool:
        full_path = self._get_full_path(bucket_name, object_name)
        if os.path.exists(full_path):
            os.remove(full_path)
            return True
        return False

    def object_exists(self, bucket_name: str, object_name: str) -> bool:
        return os.path.exists(self._get_full_path(bucket_name, object_name))

    def copy_object(
        self,
        source_bucket: str,
        source_object: str,
        dest_bucket: str,
        dest_object: str,
    ) -> str:
        src_path = self._get_full_path(source_bucket, source_object)
        if not os.path.exists(src_path):
            raise FileNotFoundError(f"Source object '{source_object}' not found in '{source_bucket}'")
        self.ensure_bucket_exists(dest_bucket)
        dst_path = self._get_full_path(dest_bucket, dest_object)
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(src_path, dst_path)
        return dest_object


class MinIOStorage(ObjectStorage):
    """Production S3-compatible object storage backed by MinIO."""

    def __init__(
        self,
        endpoint: str = "localhost:9000",
        access_key: str = "apag_admin",
        secret_key: str = "apag_secure_password_2026",
        secure: bool = False,
    ):
        from minio import Minio

        self.client = Minio(
            endpoint=endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )

    def ensure_bucket_exists(self, bucket_name: str) -> None:
        if not self.client.bucket_exists(bucket_name):
            self.client.make_bucket(bucket_name)

    def put_object(
        self,
        bucket_name: str,
        object_name: str,
        data: bytes,
        content_type: str = "application/pdf",
    ) -> str:
        self.ensure_bucket_exists(bucket_name)
        stream = io.BytesIO(data)
        self.client.put_object(
            bucket_name=bucket_name,
            object_name=object_name,
            data=stream,
            length=len(data),
            content_type=content_type,
        )
        return object_name

    def get_object(self, bucket_name: str, object_name: str) -> bytes:
        response = self.client.get_object(bucket_name, object_name)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    def delete_object(self, bucket_name: str, object_name: str) -> bool:
        try:
            self.client.remove_object(bucket_name, object_name)
            return True
        except Exception:
            return False

    def object_exists(self, bucket_name: str, object_name: str) -> bool:
        try:
            self.client.stat_object(bucket_name, object_name)
            return True
        except Exception:
            return False

    def copy_object(
        self,
        source_bucket: str,
        source_object: str,
        dest_bucket: str,
        dest_object: str,
    ) -> str:
        from minio.commonconfig import CopySource

        self.ensure_bucket_exists(dest_bucket)
        self.client.copy_object(
            dest_bucket,
            dest_object,
            CopySource(source_bucket, source_object),
        )
        return dest_object
