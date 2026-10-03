"""Pydantic request/response contracts for authentication and user identity."""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from src.db.enums import UserRole

_PASSWORD_COMPLEXITY = (
    (re.compile(r"[A-Z]"), "an uppercase letter"),
    (re.compile(r"[a-z]"), "a lowercase letter"),
    (re.compile(r"\d"), "a digit"),
    (re.compile(r"[^A-Za-z0-9]"), "a special character"),
)


class UserRegister(BaseModel):
    """Payload for creating a new employee account.

    **There is deliberately no `role` field.** It used to be here with a USER default, which
    made registration a privilege-escalation endpoint: registration is open, the field was
    caller-supplied, and posting `{"role": "ADMIN"}` created an administrator. ADMIN sees every
    RESTRICTED document in the corpus (`visible_documents_clause`), so anyone who could reach
    the API could read everything in it. Verified against the running service before the fix.

    A self-registered account is always a USER. Granting ADMIN is an administrative act and
    belongs to an endpoint that requires an existing administrator — never to the payload of
    the request creating the account.
    """

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("password")
    @classmethod
    def _password_complexity(cls, value: str) -> str:
        missing = [name for pattern, name in _PASSWORD_COMPLEXITY if not pattern.search(value)]
        if missing:
            raise ValueError(f"Password must contain at least {', '.join(missing)}.")
        return value


class UserLogin(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    """Safe user representation — never includes hashed_password."""

    user_id: uuid.UUID
    email: str
    full_name: str
    role: UserRole
    is_active: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in_minutes: int


class TokenPayload(BaseModel):
    """Decoded JWT claims."""

    sub: str  # user_id as string
    role: UserRole
    exp: int
