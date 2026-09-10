"""FastAPI dependencies for identity resolution and role enforcement.

`get_current_user` is the single seam all endpoints depend on for "who is calling this".
When SSO (Google Workspace / Azure AD) replaces JWT login later, only this function's
internals change — no endpoint signatures move.
"""

import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy.orm import Session

from src.db.engine import get_db
from src.db.models import User
from src.modules.auth.security import decode_access_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if not token:
        raise unauthorized

    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except (JWTError, KeyError, ValueError) as e:
        raise unauthorized from e

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise unauthorized
    return user
