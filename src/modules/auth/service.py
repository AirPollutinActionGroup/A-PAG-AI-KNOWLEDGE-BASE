"""User account persistence and authentication logic.

Deliberately not built on the DocumentRepository ABC/DTO pattern used elsewhere in this
codebase — that split exists so document pipeline tests can swap in an in-memory double.
User auth has no equivalent need yet (it's exercised directly against SQLite/Postgres in
tests), so a thin service over the ORM model is the simpler, correct choice for now.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core.config import settings
from src.db.enums import UserRole
from src.db.models import User
from src.modules.auth.security import hash_password, verify_password


class EmailAlreadyRegisteredError(Exception):
    pass


class InvalidCredentialsError(Exception):
    pass


class DisallowedEmailDomainError(Exception):
    pass


class AuthService:
    """Registration and authentication against the `users` table."""

    def __init__(self, db: Session):
        self.db = db

    def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email.lower())
        return self.db.execute(stmt).scalar_one_or_none()

    def get_by_id(self, user_id: uuid.UUID) -> User | None:
        stmt = select(User).where(User.user_id == user_id)
        return self.db.execute(stmt).scalar_one_or_none()

    def register(
        self,
        email: str,
        full_name: str,
        password: str,
        role: UserRole = UserRole.USER,
    ) -> User:
        if not email.lower().endswith(f"@{settings.ALLOWED_EMAIL_DOMAIN.lower()}"):
            raise DisallowedEmailDomainError(
                f"Registration is restricted to @{settings.ALLOWED_EMAIL_DOMAIN} email addresses."
            )
        if self.get_by_email(email):
            raise EmailAlreadyRegisteredError(f"Email '{email}' is already registered.")

        user = User(
            user_id=uuid.uuid4(),
            email=email.lower(),
            full_name=full_name,
            hashed_password=hash_password(password),
            role=role.value,
        )
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user

    def authenticate(self, email: str, password: str) -> User:
        user = self.get_by_email(email)
        if not user or not user.is_active or not verify_password(password, user.hashed_password):
            raise InvalidCredentialsError("Invalid email or password.")

        from sqlalchemy import func

        user.last_login_at = func.now()
        self.db.commit()
        self.db.refresh(user)
        return user
