"""Integration tests for AuditLog immutability and PostgreSQL trigger protection."""

import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.db.enums import AuditEventType
from src.modules.audit.service import AuditService


def test_postgres_audit_log_append_and_immutability(db_session: Session):
    """Verifies audit entries are inserted and trigger prevents UPDATE mutations."""
    # Ensure PostgreSQL trigger exists for immutability
    db_session.execute(text("""
        CREATE OR REPLACE FUNCTION prevent_audit_log_update()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'AUDIT_LOG_IMMUTABLE: Updates to audit_log are strictly forbidden';
        END;
        $$ LANGUAGE plpgsql;
    """))
    db_session.execute(text("""
        DROP TRIGGER IF EXISTS trg_audit_log_immutable ON audit_log;
        CREATE TRIGGER trg_audit_log_immutable
        BEFORE UPDATE ON audit_log
        FOR EACH ROW
        EXECUTE FUNCTION prevent_audit_log_update();
    """))

    doc_id = uuid.uuid4()
    corr_id = uuid.uuid4()

    entry = AuditService.log_event(
        db=db_session,
        document_id=doc_id,
        event_type=AuditEventType.DOCUMENT_QUARANTINED,
        details={"file": "test.pdf"},
        user_id="integration_tester",
        correlation_id=corr_id,
    )
    assert entry.event_id is not None

    # Attempt UPDATE -> must raise IntegrityError / InternalError
    with pytest.raises(Exception, match="AUDIT_LOG_IMMUTABLE"):
        db_session.execute(
            text("UPDATE audit_log SET event_type = 'TAMPERED' WHERE event_id = :eid"),
            {"eid": entry.event_id},
        )
        db_session.flush()

    db_session.rollback()
