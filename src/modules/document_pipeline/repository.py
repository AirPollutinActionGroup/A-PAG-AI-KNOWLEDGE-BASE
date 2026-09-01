"""PostgreSQL and In-Memory Document Repository implementations."""

import logging
import uuid
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from sqlalchemy import select
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
    def update_status(self, doc_id: uuid.UUID, status: DocumentStatus | str) -> None:
        """Updates document status."""

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
    def get_all(self) -> list[DocumentDTO]:
        """Returns all documents in the repository."""


class PostgreSQLDocumentRepository(DocumentRepository):
    """PostgreSQL implementation of DocumentRepository using SQLAlchemy Session."""

    def __init__(self, db: Session):
        self.db = db

    def _to_dto(self, orm: DocumentORM) -> DocumentDTO:
        return DocumentDTO(
            id=orm.document_id,
            filename=orm.filename,
            owner_id=orm.uploader_user_id,
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
        )

    def create(self, doc: DocumentDTO) -> DocumentDTO:
        status_val = doc.status.value if hasattr(doc.status, "value") else str(doc.status)
        class_val = doc.classification.value if hasattr(doc.classification, "value") else str(doc.classification)
        orm = DocumentORM(
            document_id=doc.id,
            filename=doc.filename,
            uploader_user_id=doc.owner_id,
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

    def update_status(self, doc_id: uuid.UUID, status: DocumentStatus | str) -> None:
        st_val = status.value if hasattr(status, "value") else str(status)
        stmt = select(DocumentORM).where(DocumentORM.document_id == doc_id)
        orm = self.db.execute(stmt).scalar_one_or_none()
        if orm:
            orm.status = st_val
            self.db.commit()

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
            self.db.commit()
            self.db.refresh(orm)
            return self._to_dto(orm)
        return self.create(doc)

    def get_all(self) -> list[DocumentDTO]:
        stmt = select(DocumentORM).order_by(DocumentORM.created_at.desc())
        orms = self.db.execute(stmt).scalars().all()
        return [self._to_dto(o) for o in orms]


import threading


class InMemoryDocumentRepository(DocumentRepository):
    """In-memory repository implementation for unit and concurrency tests."""

    def __init__(self):
        self._storage: dict[uuid.UUID, DocumentDTO] = {}
        self._lock = threading.Lock()

    def create(self, doc: DocumentDTO) -> DocumentDTO:
        with self._lock:
            self._storage[doc.id] = doc.model_copy(deep=True)
            return self._storage[doc.id]

    def update_status(self, doc_id: uuid.UUID, status: DocumentStatus | str) -> None:
        with self._lock:
            if doc_id in self._storage:
                st = status if isinstance(status, DocumentStatus) else DocumentStatus(status)
                self._storage[doc_id].status = st
                self._storage[doc_id].updated_at = datetime.now(UTC)

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

    def get_all(self) -> list[DocumentDTO]:
        with self._lock:
            return [doc.model_copy(deep=True) for doc in self._storage.values()]

