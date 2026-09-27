"""The document visibility rule, and the agreement between its two forms.

The rule exists twice by necessity: a Python predicate for a document already loaded, and a SQL
expression for queries that must filter before `ORDER BY`/`LIMIT`. Two spellings of one policy
drift, and the drift is silent — it shows up as somebody seeing a document they shouldn't, not as
a failing assertion. So the important tests here are the equivalence ones, which enumerate every
combination and require both forms to agree.

The SQL form runs against SQLite. That is enough: what is under test is the boolean logic the
expression compiles to, not a Postgres-specific feature.
"""

import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from src.db.enums import Classification, UserRole
from src.db.models import Base
from src.db.models import Document as DocumentORM
from src.modules.auth.access import can_view, is_admin, visible_documents_clause

OWNER = uuid.uuid4()
STRANGER = uuid.uuid4()


@pytest.fixture
def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'access.db'}")
    Base.metadata.create_all(bind=engine)
    return sessionmaker(bind=engine)


# ==============================================================================
# is_admin
# ==============================================================================

def test_only_the_admin_role_is_admin():
    assert is_admin(UserRole.ADMIN.value) is True
    assert is_admin(UserRole.USER.value) is False
    assert is_admin(None) is False
    assert is_admin("admin") is False, "the check is exact, not case-insensitive"


# ==============================================================================
# The Python form
# ==============================================================================

def test_public_is_visible_to_anyone():
    assert can_view(Classification.PUBLIC, OWNER, STRANGER, viewer_is_admin=False) is True


def test_restricted_is_hidden_from_a_stranger():
    assert can_view(Classification.RESTRICTED, OWNER, STRANGER, viewer_is_admin=False) is False


def test_restricted_is_visible_to_its_owner():
    assert can_view(Classification.RESTRICTED, OWNER, OWNER, viewer_is_admin=False) is True


def test_restricted_is_visible_to_an_admin():
    assert can_view(Classification.RESTRICTED, OWNER, STRANGER, viewer_is_admin=True) is True


def test_an_ownerless_restricted_document_is_admin_only():
    """A RESTRICTED row with no uploader must not become visible to whoever also has no id —
    `None == None` would otherwise read as ownership."""
    assert can_view(Classification.RESTRICTED, None, None, viewer_is_admin=False) is False
    assert can_view(Classification.RESTRICTED, None, None, viewer_is_admin=True) is True


def test_a_null_classification_is_treated_as_visible():
    """Matches the column default. A row that somehow has no classification should not become
    invisible to everyone, including its owner."""
    assert can_view(None, OWNER, STRANGER, viewer_is_admin=False) is True


# ==============================================================================
# The two forms must agree — this is the point of the module
# ==============================================================================

CASES = [
    pytest.param(c, o, v, a, id=f"{c or 'NULL'}-{'owner' if o else 'noowner'}"
                                f"-{'self' if o and o == v else 'other'}-{'admin' if a else 'user'}")
    for c in (Classification.PUBLIC.value, Classification.RESTRICTED.value, None)
    for o, v in ((OWNER, OWNER), (OWNER, STRANGER), (None, STRANGER), (None, None))
    for a in (False, True)
]


@pytest.mark.parametrize("classification,owner,viewer,admin", CASES)
def test_sql_and_python_forms_agree(session_factory, classification, owner, viewer, admin):
    """Enumerates every combination. A disagreement here is a document shown to the wrong person
    by one code path and not the other — which is precisely the bug that cannot be caught by
    testing either form alone."""
    doc_id = uuid.uuid4()
    with session_factory() as s:
        s.add(DocumentORM(
            document_id=doc_id, filename="doc.pdf", file_size=1, status="LIVE",
            classification=classification, uploader_user_id=owner,
        ))
        s.commit()

    with session_factory() as s:
        rows = s.execute(
            select(DocumentORM.document_id).where(
                visible_documents_clause(DocumentORM, viewer, viewer_is_admin=admin)
            )
        ).scalars().all()
    sql_says_visible = doc_id in rows

    python_says_visible = can_view(
        Classification(classification) if classification else None,
        owner, viewer, viewer_is_admin=admin,
    )

    assert sql_says_visible == python_says_visible, (
        f"drift: SQL={sql_says_visible} Python={python_says_visible} "
        f"for classification={classification} owner={owner} viewer={viewer} admin={admin}"
    )


# ==============================================================================
# The SQL form on its own
# ==============================================================================

def _seed(session: Session) -> dict[str, uuid.UUID]:
    ids = {
        "public": uuid.uuid4(),
        "restricted_owned": uuid.uuid4(),
        "restricted_other": uuid.uuid4(),
        "null_class": uuid.uuid4(),
    }
    session.add_all([
        DocumentORM(document_id=ids["public"], filename="p.pdf", file_size=1, status="LIVE",
                    classification="PUBLIC", uploader_user_id=STRANGER),
        DocumentORM(document_id=ids["restricted_owned"], filename="ro.pdf", file_size=1,
                    status="LIVE", classification="RESTRICTED", uploader_user_id=OWNER),
        DocumentORM(document_id=ids["restricted_other"], filename="rx.pdf", file_size=1,
                    status="LIVE", classification="RESTRICTED", uploader_user_id=STRANGER),
        DocumentORM(document_id=ids["null_class"], filename="n.pdf", file_size=1, status="LIVE",
                    classification=None, uploader_user_id=STRANGER),
    ])
    session.commit()
    return ids


def _visible(session, viewer, admin) -> set[uuid.UUID]:
    return set(session.execute(
        select(DocumentORM.document_id).where(
            visible_documents_clause(DocumentORM, viewer, viewer_is_admin=admin)
        )
    ).scalars().all())


def test_sql_scopes_a_normal_user_to_public_plus_their_own(session_factory):
    with session_factory() as s:
        ids = _seed(s)
        got = _visible(s, OWNER, admin=False)

    assert got == {ids["public"], ids["null_class"], ids["restricted_owned"]}
    assert ids["restricted_other"] not in got


def test_sql_gives_an_admin_everything(session_factory):
    with session_factory() as s:
        ids = _seed(s)
        assert _visible(s, STRANGER, admin=True) == set(ids.values())


def test_sql_gives_an_anonymous_viewer_only_unrestricted_rows(session_factory):
    """`viewer_id=None` owns nothing. Emitting `uploader_user_id = NULL` would be NULL for every
    row rather than true, but the ownership term is dropped entirely so the intent is explicit."""
    with session_factory() as s:
        ids = _seed(s)
        got = _visible(s, None, admin=False)

    assert got == {ids["public"], ids["null_class"]}


def test_admin_clause_is_a_predicate_not_an_omission(session_factory):
    """An admin gets a literal TRUE rather than no clause, so callers can AND it in
    unconditionally and cannot forget it on one branch."""
    clause = visible_documents_clause(DocumentORM, None, viewer_is_admin=True)
    assert clause is not None
    with session_factory() as s:
        ids = _seed(s)
        assert _visible(s, None, admin=True) == set(ids.values())
