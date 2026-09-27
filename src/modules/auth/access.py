"""The document visibility rule, spelled once.

`PUBLIC` is org-wide; `RESTRICTED` is visible to its uploader and to any `ADMIN`. Deliberately a
2-tier owner-scoped model rather than per-document ACLs — see `ARCHITECTURE.md` §6b.

The rule needs two forms: a Python predicate for a document already in hand, and a SQL expression
for queries that must filter *before* `ORDER BY`/`LIMIT`. Those had drifted into three separate
spellings — `_can_view()` in the API, a post-query list comprehension in the list/search endpoints,
and hand-written SQL in retrieval. This module is the one definition both forms derive from, so a
policy change cannot land in one place and not the others.

**Filtering must happen in SQL, not after it.** Post-filtering a page of results is not merely
untidy: restricted rows consume slots in the page, the `total` returned to the client counts rows
the caller may not see, and for a top-k similarity search a restricted row removed after ranking
has already taken its slot — so `limit=5` returns four results, or none, and the caller cannot
distinguish a thin corpus from a withheld answer.

The two forms must agree on the awkward cases, and they are tested against each other in
`tests/unit/test_access_rule.py`:

- **NULL classification** is visible. The Python form falls out of `!= RESTRICTED`, but plain SQL
  `classification <> 'RESTRICTED'` evaluates to NULL — not true — and would hide the row. The SQL
  form therefore uses `IS DISTINCT FROM`.
- **An anonymous viewer** (`viewer_id is None`) owns nothing. Emitting
  `uploader_user_id = NULL` would be NULL for every row, but emitting it at all invites the
  mistake, so the ownership term is dropped entirely in that case.
"""

import uuid

from sqlalchemy import ColumnElement, or_, true

from src.db.enums import Classification, UserRole

_RESTRICTED = Classification.RESTRICTED.value


def is_admin(role: str | None) -> bool:
    """The single spelling of the admin check."""
    return role == UserRole.ADMIN.value


def can_view(
    classification: str | Classification | None,
    owner_id: uuid.UUID | None,
    viewer_id: uuid.UUID | None,
    *,
    viewer_is_admin: bool,
) -> bool:
    """Python form of the rule, for a document already loaded."""
    if classification != Classification.RESTRICTED:
        return True
    if viewer_is_admin:
        return True
    return owner_id is not None and owner_id == viewer_id


def visible_documents_clause(
    document_model,
    viewer_id: uuid.UUID | None,
    *,
    viewer_is_admin: bool,
) -> ColumnElement[bool]:
    """SQL form of the same rule, for a `WHERE` clause.

    `document_model` is passed in rather than imported so this module carries no ORM dependency —
    each caller hands in the mapped class or alias it is already querying.

    An admin gets a literal `TRUE` rather than no predicate at all, so callers can `AND` this in
    unconditionally and cannot forget it on one branch.
    """
    if viewer_is_admin:
        return true()

    # IS DISTINCT FROM, not <>: a NULL classification must read as visible, matching can_view().
    not_restricted = document_model.classification.is_distinct_from(_RESTRICTED)

    if viewer_id is None:
        return not_restricted

    return or_(not_restricted, document_model.uploader_user_id == viewer_id)
