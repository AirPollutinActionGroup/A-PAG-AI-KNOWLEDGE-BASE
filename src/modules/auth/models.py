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
    """Payload for creating a new employee account."""

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=8, max_length=128)
    role: UserRole = UserRole.USER

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
