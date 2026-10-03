"""Authentication endpoints: register, login, and identity introspection."""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from src.core.config import settings
from src.db.engine import get_db
from src.db.enums import UserRole
from src.db.models import User
from src.modules.auth.dependencies import get_current_user
from src.modules.auth.models import Token, UserOut, UserRegister
from src.modules.auth.security import create_access_token
from src.modules.auth.service import (
    AuthService,
    DisallowedEmailDomainError,
    EmailAlreadyRegisteredError,
    InvalidCredentialsError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: UserRegister, db: Session = Depends(get_db)):
    """Creates a new employee account.

    Always creates a **USER**. The role is not caller-supplied: with open registration a
    caller-supplied role meant anyone who could reach this endpoint could make themselves an
    administrator and read every RESTRICTED document in the corpus.

    NOTE: open registration itself is still intentional for the 50-person internal-org
    bootstrap phase. Before any wider or external rollout, gate this behind admin invite / SSO.
    With the role fixed, the remaining exposure is that a stranger who reaches the API can see
    PUBLIC documents — bad, but bounded, and not the whole corpus.
    """
    service = AuthService(db)
    try:
        user = service.register(
            email=payload.email,
            full_name=payload.full_name,
            password=payload.password,
            # Never from the payload. See UserRegister: `role` there made registration a
            # privilege-escalation endpoint, because registration is open and ADMIN can read
            # every RESTRICTED document in the corpus.
            role=UserRole.USER,
        )
    except EmailAlreadyRegisteredError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e)) from e
    except DisallowedEmailDomainError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e)) from e
    return user


@router.post("/login", response_model=Token)
async def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    """OAuth2 password flow: form fields are `username` (email) and `password`."""
    service = AuthService(db)
    try:
        user = service.authenticate(form_data.username, form_data.password)
    except InvalidCredentialsError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(e),
            headers={"WWW-Authenticate": "Bearer"},
        ) from e

    token = create_access_token(user.user_id, user.role)
    return Token(
        access_token=token,
        expires_in_minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES,
    )


@router.get("/demo-login")
async def demo_login(db: Session = Depends(get_db)):
    """The shared demo credentials, when this deployment has configured them.

    Exists so the sign-in page can show a login for people trying the system, without the
    password living in the repository. Deliberately unauthenticated, since it is shown to someone
    who has not signed in yet, which is why it will not hand out the credentials of an
    administrator: that account can read every RESTRICTED document, and publishing its password
    on an open page would defeat the access model. Misconfiguration fails closed (404).
    """
    email, password = settings.DEMO_LOGIN_EMAIL, settings.DEMO_LOGIN_PASSWORD
    if not (email and password):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No demo login is configured.")

    account = db.query(User).filter(User.email == email.lower()).first()
    if account is not None and account.role == UserRole.ADMIN:
        logger.error("DEMO_LOGIN_EMAIL names an ADMIN account; refusing to publish its password")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No demo login is configured.")

    return {"email": email, "password": password}


@router.get("/me", response_model=UserOut)
async def read_current_user(current_user: User = Depends(get_current_user)):
    return current_user
