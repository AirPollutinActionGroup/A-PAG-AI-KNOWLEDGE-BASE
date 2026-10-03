"""The shared demo login shown under the sign-in box.

The endpoint is unauthenticated on purpose, because it is read by someone who has not signed in
yet, and that makes it the one place this API hands out a password. Two properties keep that from
being a hole: it exists only when a deployment configures it, and it will not name an
administrator, since ADMIN reads every RESTRICTED document and publishing that password on an
open page would defeat the access model.
"""

import asyncio

import pytest
from fastapi import HTTPException

import src.api.v1.auth as auth_module
from src.db.enums import UserRole


class FakeDb:
    """Stands in for the Session: `.query(User).filter(...).first()` returns `account`."""

    def __init__(self, account=None):
        self._account = account

    def query(self, _model):
        return self

    def filter(self, *_criteria):
        return self

    def first(self):
        return self._account


def account(role):
    return type("U", (), {"role": role})()


def call(db, monkeypatch, email, password):
    monkeypatch.setattr(auth_module.settings, "DEMO_LOGIN_EMAIL", email)
    monkeypatch.setattr(auth_module.settings, "DEMO_LOGIN_PASSWORD", password)
    return asyncio.run(auth_module.demo_login(db))


def test_nothing_is_published_when_the_deployment_has_not_configured_one(monkeypatch):
    """The normal case, and the default: the sign-in page shows no credentials."""
    with pytest.raises(HTTPException) as exc:
        call(FakeDb(), monkeypatch, "", "")
    assert exc.value.status_code == 404


@pytest.mark.parametrize("email, password", [("demo@a-pag.org", ""), ("", "Str0ng!Passphrase")])
def test_half_a_configuration_publishes_nothing(monkeypatch, email, password):
    """An email with no password, or the reverse, is a misconfiguration, not a partial login."""
    with pytest.raises(HTTPException) as exc:
        call(FakeDb(), monkeypatch, email, password)
    assert exc.value.status_code == 404


def test_a_configured_user_account_is_published(monkeypatch):
    out = call(FakeDb(account(UserRole.USER)), monkeypatch, "demo@a-pag.org", "Str0ng!Passphrase")

    assert out == {"email": "demo@a-pag.org", "password": "Str0ng!Passphrase"}


def test_an_account_not_yet_created_is_still_published(monkeypatch):
    """Config can be set before the account exists. Refusing here would only make the order of
    two setup steps matter."""
    out = call(FakeDb(None), monkeypatch, "demo@a-pag.org", "Str0ng!Passphrase")

    assert out["email"] == "demo@a-pag.org"


def test_an_administrators_credentials_are_never_published(monkeypatch):
    """The test that matters. Pointing this at an admin by mistake must fail closed and look
    exactly like 'not configured', so the response gives away nothing about why."""
    with pytest.raises(HTTPException) as exc:
        call(FakeDb(account(UserRole.ADMIN)), monkeypatch, "boss@a-pag.org", "Str0ng!Passphrase")

    assert exc.value.status_code == 404
    assert "admin" not in str(exc.value.detail).lower()


def test_the_response_is_identical_for_an_admin_and_for_no_configuration(monkeypatch):
    """Otherwise the status text would let a stranger learn which account is the administrator."""
    with pytest.raises(HTTPException) as unconfigured:
        call(FakeDb(), monkeypatch, "", "")
    with pytest.raises(HTTPException) as admin:
        call(FakeDb(account(UserRole.ADMIN)), monkeypatch, "boss@a-pag.org", "Str0ng!Passphrase")

    assert unconfigured.value.detail == admin.value.detail
