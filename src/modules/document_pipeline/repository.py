"""PostgreSQL and In-Memory Document Repository implementations."""

import logging
import threading
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.db.enums import Classification, DocumentStatus
from src.db.models import Document as DocumentORM
from src.modules.document_pipeline.models import Document as DocumentDTO

logger = logging.getLogger(__name__)


class DocumentRepository(ABC):
    """Abstract interface for Document persistence."""

    @abstractmethod
    def create(self, doc: DocumentDTO) -> DocumentDTO:
        """Saves a new document record."""

    @abstractmethod
    def get_by_id(self, doc_id: uuid.UUID) -> DocumentDTO | None:
        """Retrieves document record by ID."""

    @abstractmethod
    def get_by_checksum(self, checksum: str) -> DocumentDTO | None:
        """Retrieves document by SHA-256 checksum (ignoring SUPERSEDED and ARCHIVED)."""

    @abstractmethod
    def update_document(self, doc: DocumentDTO) -> DocumentDTO:
        """Updates full document record."""

    @abstractmethod
    def list_paginated(self, limit: int = 50, offset: int = 0) -> tuple[list[DocumentDTO], int]:
        """Returns (page of documents, total count), newest first."""

    @abstractmethod
    def search(self, query: str, limit: int = 50, offset: int = 0) -> tuple[list[DocumentDTO], int]:
        """Full-text search over title/filename/description. Returns (page, total count)."""

    @abstractmethod
    def purge(self, doc_id: uuid.UUID) -> bool:
        """Tombstones a document whose bytes have already been erased from object storage:
        stamps deleted_at/purged_at and nulls sha256 + raw_path. Nulling sha256 is what frees
        the same file to be re-uploaded later, since the dedup index is partial on
        `sha256 IS NOT NULL`. Returns False if the document doesn't exist or is already purged.

        Callers must delete the object from storage *before* calling this — a failed storage
        delete must leave the row un-tombstoned so the operation stays retryable."""


def _to_dto(orm: DocumentORM) -> DocumentDTO:
    return DocumentDTO(
        id=orm.document_id,
        filename=orm.filename,
        owner_id=orm.uploader_user_id,
        title=orm.title,
        description=orm.description,
        mime_type=orm.mime_type,
        page_count=orm.page_count,
        upload_batch_id=orm.upload_batch_id,
        size=orm.file_size,
        checksum=orm.sha256,
        status=DocumentStatus(orm.status),
        classification=Classification(orm.classification) if orm.classification else None,
        version=orm.version,
        supersedes_id=orm.supersedes_id,
        quarantine_path=orm.quarantine_path,
        raw_path=orm.raw_path,
        rejection_reason=orm.rejection_reason,
        created_at=orm.created_at,
        updated_at=orm.updated_at,
        deleted_at=orm.deleted_at,
        purged_at=orm.purged_at,
    )


class PostgreSQLDocumentRepository(DocumentRepository):
    """PostgreSQL implementation of DocumentRepository using SQLAlchemy Session."""

    def __init__(self, db: Session):
        self.db = db

    def _to_dto(self, orm: DocumentORM) -> DocumentDTO:
        return _to_dto(orm)

    def create(self, doc: DocumentDTO) -> DocumentDTO:
        status_val = doc.status.value if hasattr(doc.status, "value") else str(doc.status)
        class_val = doc.classification.value if hasattr(doc.classification, "value") else str(doc.classification)
        orm = DocumentORM(
            document_id=doc.id,
            filename=doc.filename,
            uploader_user_id=doc.owner_id,
            title=doc.title,
            description=doc.description,
            mime_type=doc.mime_type,
            page_count=doc.page_count,
            upload_batch_id=doc.upload_batch_id,
            file_size=doc.size,
            sha256=doc.checksum,
            status=status_val,
            classification=class_val,
            version=doc.version,
            supersedes_id=doc.supersedes_id,
            quarantine_path=doc.quarantine_path,
            raw_path=doc.raw_path,
            rejection_reason=doc.rejection_reason,
        )
        self.db.add(orm)
        self.db.commit()
        self.db.refresh(orm)
        return self._to_dto(orm)

    def get_by_id(self, doc_id: uuid.UUID) -> DocumentDTO | None:
        stmt = select(DocumentORM).where(DocumentORM.document_id == doc_id)
        orm = self.db.execute(stmt).scalar_one_or_none()
        return self._to_dto(orm) if orm else None

    def get_by_checksum(self, checksum: str) -> DocumentDTO | None:
        stmt = select(DocumentORM).where(
            DocumentORM.sha256 == checksum,
            DocumentORM.status.not_in([DocumentStatus.SUPERSEDED.value, DocumentStatus.ARCHIVED.value]),
        )
        orm = self.db.execute(stmt).scalars().first()
        return self._to_dto(orm) if orm else None

    def update_document(self, doc: DocumentDTO) -> DocumentDTO:
        stmt = select(DocumentORM).where(DocumentORM.document_id == doc.id)
        orm = self.db.execute(stmt).scalar_one_or_none()
        if orm:
            orm.status = doc.status.value if hasattr(doc.status, "value") else str(doc.status)
            if hasattr(doc, "classification") and doc.classification:
                orm.classification = doc.classification.value if hasattr(doc.classification, "value") else str(doc.classification)
            orm.sha256 = doc.checksum
            orm.raw_path = doc.raw_path
            orm.quarantine_path = doc.quarantine_path
            orm.rejection_reason = doc.rejection_reason
            orm.version = doc.version
            orm.supersedes_id = doc.supersedes_id
            orm.title = doc.title
            orm.description = doc.description
            orm.page_count = doc.page_count
            orm.upload_batch_id = doc.upload_batch_id
            self.db.commit()
            self.db.refresh(orm)
            return self._to_dto(orm)
        return self.create(doc)

    def list_paginated(self, limit: int = 50, offset: int = 0) -> tuple[list[DocumentDTO], int]:
        base = select(DocumentORM).where(DocumentORM.deleted_at.is_(None))
        total = self.db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        stmt = base.order_by(DocumentORM.created_at.desc()).limit(limit).offset(offset)
        orms = self.db.execute(stmt).scalars().all()
        return [self._to_dto(o) for o in orms], total

    def search(self, query: str, limit: int = 50, offset: int = 0) -> tuple[list[DocumentDTO], int]:
        from sqlalchemy import text

        ts_query = func.plainto_tsquery("english", query)
        base = select(DocumentORM).where(
            DocumentORM.deleted_at.is_(None),
            DocumentORM.search_vector.op("@@")(ts_query),
        )
        total = self.db.execute(select(func.count()).select_from(base.subquery())).scalar_one()
        stmt = (
            base.order_by(text("ts_rank(search_vector, plainto_tsquery('english', :q)) DESC"))
            .limit(limit)
            .offset(offset)
        )
        orms = self.db.execute(stmt, {"q": query}).scalars().all()
        return [self._to_dto(o) for o in orms], total

    def purge(self, doc_id: uuid.UUID) -> bool:
        stmt = select(DocumentORM).where(
            DocumentORM.document_id == doc_id, DocumentORM.purged_at.is_(None)
        )
        orm = self.db.execute(stmt).scalar_one_or_none()
        if orm is None:
            return False
        now = datetime.now(UTC)
        orm.purged_at = now
        if orm.deleted_at is None:
            orm.deleted_at = now
        orm.sha256 = None
        orm.raw_path = None
        orm.quarantine_path = None
        self.db.commit()
        return True


class InMemoryDocumentRepository(DocumentRepository):
    """In-memory repository implementation for unit and concurrency tests."""

    def __init__(self):
        self._storage: dict[uuid.UUID, DocumentDTO] = {}
        self._lock = threading.Lock()

    def create(self, doc: DocumentDTO) -> DocumentDTO:
        with self._lock:
            self._storage[doc.id] = doc.model_copy(deep=True)
            return self._storage[doc.id]

    def get_by_id(self, doc_id: uuid.UUID) -> DocumentDTO | None:
        with self._lock:
            if doc_id in self._storage:
                return self._storage[doc_id].model_copy(deep=True)
            return None

    def get_by_checksum(self, checksum: str) -> DocumentDTO | None:
        with self._lock:
            for doc in self._storage.values():
                if doc.checksum == checksum and doc.status not in [
                    DocumentStatus.SUPERSEDED,
                    DocumentStatus.ARCHIVED,
                    DocumentStatus.REJECTED,
                    DocumentStatus.DUPLICATE,
                ]:
                    return doc.model_copy(deep=True)
            return None

    def update_document(self, doc: DocumentDTO) -> DocumentDTO:
        with self._lock:
            doc.updated_at = datetime.now(UTC)
            self._storage[doc.id] = doc.model_copy(deep=True)
            return self._storage[doc.id]

    def list_paginated(self, limit: int = 50, offset: int = 0) -> tuple[list[DocumentDTO], int]:
        with self._lock:
            docs = sorted(
                (d for d in self._storage.values() if d.deleted_at is None),
                key=lambda d: d.created_at, reverse=True,
            )
            total = len(docs)
            page = docs[offset : offset + limit]
            return [d.model_copy(deep=True) for d in page], total

    def search(self, query: str, limit: int = 50, offset: int = 0) -> tuple[list[DocumentDTO], int]:
        q = query.lower()
        with self._lock:
            matches = [
                d
                for d in self._storage.values()
                if d.deleted_at is None
                and (
                    q in (d.title or "").lower()
                    or q in d.filename.lower()
                    or q in (d.description or "").lower()
                )
            ]
            matches.sort(key=lambda d: d.created_at, reverse=True)
            total = len(matches)
            page = matches[offset : offset + limit]
            return [d.model_copy(deep=True) for d in page], total

    def purge(self, doc_id: uuid.UUID) -> bool:
        with self._lock:
            doc = self._storage.get(doc_id)
            if doc is None or doc.purged_at is not None:
                return False
            now = datetime.now(UTC)
            doc.purged_at = now
            if doc.deleted_at is None:
                doc.deleted_at = now
            doc.checksum = None
            doc.raw_path = None
            doc.quarantine_path = None
            return True
