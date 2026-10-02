"""Self-registration cannot grant a role.

This is a regression test for a live privilege escalation, verified against the running service
before it was fixed: `UserRegister` carried a caller-supplied `role` field, registration is open,
and `POST /auth/register {"role": "ADMIN"}` returned 201 with an administrator account. ADMIN
bypasses the RESTRICTED filter in `visible_documents_clause`, so anyone who could reach the API
could read every restricted document in the corpus.

The fix is that the field does not exist. That is worth a test rather than trusting the model
definition, because "someone adds a convenient field back" is exactly how this class of bug
returns — and a stray `role` would be silently accepted by pydantic's default config.
"""

import uuid

import pytest
from pydantic import ValidationError

from src.db.enums import UserRole
from src.modules.auth.models import UserRegister

VALID = {
    "email": "someone@a-pag.org",
    "full_name": "Some One",
    "password": "Str0ng!Passphrase",
}


def test_the_payload_has_no_role_field():
    """The model must not carry it at all. A field with a USER default is still the bug: the
    default only applies when the caller stays silent, and an attacker does not."""
    assert "role" not in UserRegister.model_fields


def test_a_role_in_the_payload_does_not_become_a_role():
    """Whether pydantic ignores or rejects it, what must never happen is it taking effect."""
    try:
        parsed = UserRegister(**VALID, role="ADMIN")
    except ValidationError:
        return  # rejected outright is also fine
    assert not hasattr(parsed, "role"), "a supplied role must not survive parsing"


def test_registration_always_creates_a_user(monkeypatch):
    """The endpoint pins the role itself rather than reading it from anywhere."""
    import src.api.v1.auth as auth_module

    captured = {}

    class FakeService:
        def __init__(self, _db):
            pass

        def register(self, **kwargs):
            captured.update(kwargs)
            return type("U", (), {
                "user_id": uuid.uuid4(), "email": kwargs["email"],
                "full_name": kwargs["full_name"], "role": kwargs["role"],
                "is_active": True, "created_at": None,
            })()

    monkeypatch.setattr(auth_module, "AuthService", FakeService)

    import asyncio
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        auth_module.register(UserRegister(**VALID), db=None)
    )

    assert captured["role"] == UserRole.USER


@pytest.mark.parametrize("role", ["ADMIN", "admin", UserRole.ADMIN.value])
def test_no_spelling_of_admin_gets_through(role):
    try:
        parsed = UserRegister(**VALID, role=role)
    except ValidationError:
        return
    assert getattr(parsed, "role", None) != "ADMIN"
