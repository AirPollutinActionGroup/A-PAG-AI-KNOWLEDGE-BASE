"""Password hashing and JWT issuance/verification.

Uses the `bcrypt` library directly rather than passlib's CryptContext wrapper: passlib 1.7.4
(unmaintained since 2020) crashes against bcrypt>=4.1's changed API (its internal
"wrap bug" backend-detection routine raises ValueError on first use). Calling bcrypt
directly is also simpler — there's no scheme-negotiation layer to configure.
"""

import uuid
from datetime import UTC, datetime, timedelta

import bcrypt
from jose import JWTError, jwt

from src.core.config import settings
from src.db.enums import UserRole

# bcrypt silently truncates/errors past 72 bytes; reject clearly instead of surprising behavior.
_MAX_PASSWORD_BYTES = 72


def hash_password(plain_password: str) -> str:
    pw_bytes = plain_password.encode("utf-8")
    if len(pw_bytes) > _MAX_PASSWORD_BYTES:
        raise ValueError(f"Password must be at most {_MAX_PASSWORD_BYTES} bytes.")
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: uuid.UUID, role: UserRole | str) -> str:
    role_val = role.value if isinstance(role, UserRole) else role
    expire = datetime.now(UTC) + timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    claims = {"sub": str(user_id), "role": role_val, "exp": expire}
    return jwt.encode(claims, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """Raises jose.JWTError (or subclasses, e.g. ExpiredSignatureError) on any invalid token."""
    return jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])


__all__ = [
    "JWTError",
    "create_access_token",
    "decode_access_token",
    "hash_password",
    "verify_password",
]
