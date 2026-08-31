"""Audit service for writing immutable audit trail entries."""

import logging
import uuid
from typing import Any

from sqlalchemy.orm import Session

from src.db.enums import AuditEventType
from src.db.models import AuditLog

logger = logging.getLogger(__name__)


class AuditService:
    """Provides methods for recording document and pipeline lifecycle events."""

    @staticmethod
    def log_event(
        db: Session,
        document_id: uuid.UUID,
        event_type: AuditEventType | str,
        details: dict[str, Any] | None = None,
        user_id: str | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> AuditLog:
        """Records an immutable audit entry."""
        ev_type_str = event_type.value if isinstance(event_type, AuditEventType) else str(event_type)
        entry = AuditLog(
            document_id=document_id,
            user_id=user_id,
            event_type=ev_type_str,
            details=details,
            correlation_id=correlation_id,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        logger.info(
            "Audit entry: event_id=%s doc_id=%s type=%s user=%s",
            entry.event_id, document_id, ev_type_str, user_id,
        )
        return entry
