from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from backend.api.deps import CurrentUser, SessionDep
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.rate_limit import limiter
from backend.core.security import create_access_token, hash_password, verify_password
from backend.db.models import UserRole
from backend.db.repositories.users import UserRepository
from backend.models.auth import TokenResponse, UserOut
from pydantic import BaseModel

logger = get_logger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])



@router.post("/login", response_model=TokenResponse)
@limiter.limit(get_settings().rate_limit_login)
async def login(
    request: Request,
    response: Response,  # required by slowapi to inject X-RateLimit-* headers
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    session: SessionDep,
) -> TokenResponse:
    raw = form.username.strip()
    settings = get_settings()

    # ── Path 1: email ──────────────────────────────────────
    if "@" in raw:
        repo = UserRepository(session)
        user = await repo.get_by_email(raw)
        if user is None or not user.is_active or not verify_password(form.password, user.password_hash):
            logger.info("auth.login.failed", username=raw)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
        token = create_access_token(
            subject=str(user.id), role=user.role.value,
            tenant_id=str(user.tenant_id) if user.tenant_id else None,
        )
        logger.info("auth.login.ok", user_id=str(user.id), role=user.role.value)
        return TokenResponse(access_token=token, expires_in_min=settings.jwt_access_token_ttl_min)

    # ── Path 2: parent phone — try when the input has ≥10 digits ────
    # Accepts any common format: "9990003333", "+91 9990003333",
    # "+919990003333", "91-99900-03333", etc. We strip non-digits before
    # lookup and use the trailing 10 digits as the match key. If the phone
    # path doesn't find anyone, we fall through to the handle path
    # because a student roll number could also be numeric.
    digits_only = ''.join(c for c in raw if c.isdigit())
    if len(digits_only) >= 10:
        repo = UserRepository(session)
        user = await repo.get_by_phone_digits(digits_only)
        if user is not None and user.is_active and verify_password(form.password, user.password_hash):
            token = create_access_token(
                subject=str(user.id), role=user.role.value,
                tenant_id=str(user.tenant_id) if user.tenant_id else None,
            )
            logger.info("auth.login.ok", user_id=str(user.id), role=user.role.value)
            return TokenResponse(access_token=token, expires_in_min=settings.jwt_access_token_ttl_min)

    # ── Path 3: student roll-number ────────────────────────
    # Roll-number resolves directly to a dedicated User row (role='student',
    # handle=roll_no) created during onboarding. No masquerade — the view is
    # selected by user.role exactly like teacher / school_admin.
    repo = UserRepository(session)
    user = await repo.get_by_handle(raw)
    if (
        user is None
        or not user.is_active
        or user.role is not UserRole.student
        or not verify_password(form.password, user.password_hash)
    ):
        logger.info("auth.login.failed", username=raw)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")

    token = create_access_token(
        subject=str(user.id), role=user.role.value,
        tenant_id=str(user.tenant_id) if user.tenant_id else None,
    )
    logger.info("auth.login.ok", user_id=str(user.id), role=user.role.value, via="handle")
    return TokenResponse(access_token=token, expires_in_min=settings.jwt_access_token_ttl_min)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)


class ProfilePatch(BaseModel):
    full_name: str | None = None
    current_password: str | None = None
    new_password: str | None = None


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: ProfilePatch, user: CurrentUser, session: SessionDep
) -> UserOut:
    """Self-service profile update: name and password.

    Changing the password requires the current password (so a stolen JWT
    can't take over the account on its own).
    """
    if body.full_name is not None:
        name = body.full_name.strip()
        if not name:
            raise HTTPException(400, "Name cannot be blank.")
        if len(name) > 200:
            raise HTTPException(400, "Name is too long.")
        user.full_name = name

    if body.new_password is not None:
        if not body.current_password:
            raise HTTPException(400, "Current password is required to change password.")
        if not verify_password(body.current_password, user.password_hash):
            raise HTTPException(401, "Current password is incorrect.")
        if len(body.new_password) < 8:
            raise HTTPException(400, "New password must be at least 8 characters.")
        user.password_hash = hash_password(body.new_password)

    await session.flush()
    return UserOut.model_validate(user)
