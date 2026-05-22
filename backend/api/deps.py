from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from backend.core.security import decode_token
from backend.db.base import get_session_factory
from backend.db.models import Student, User, UserRole
from backend.db.repositories.users import UserRepository

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


async def get_db_session() -> AsyncIterator[AsyncSession]:
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_db_session)]


async def get_current_user(
    session: SessionDep,
    token: Annotated[str, Depends(oauth2_scheme)],
) -> User:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="invalid or expired credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
    except ValueError as exc:
        raise credentials_exc from exc

    sub = payload.get("sub")
    if not sub:
        raise credentials_exc
    try:
        user_id = uuid.UUID(sub)
    except ValueError as exc:
        raise credentials_exc from exc

    repo = UserRepository(session)
    user = await repo.get_by_id(user_id)
    if user is None or not user.is_active:
        raise credentials_exc

    # Token role must match DB role exactly — no view-override masquerade.
    # The view in the SPA is derived from user.role (parent → parent view,
    # student → student view), exactly like teacher / school_admin.
    token_role = payload.get("role")
    if token_role and token_role != user.role.value:
        raise credentials_exc

    # For student users, resolve which Student row they back so existing
    # endpoints can read `user._student_id` without an extra join per call.
    # The Student.user_id link is one-to-one and indexed.
    if user.role is UserRole.student:
        sid = await session.scalar(
            select(Student.id).where(Student.user_id == user.id)
        )
        if sid is not None:
            user._student_id = sid  # type: ignore[attr-defined]

    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*roles: UserRole):
    allowed: set[UserRole] = set(roles)

    async def dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role '{user.role.value}' not permitted",
            )
        return user

    return dependency


SuperAdminUser = Annotated[User, Depends(require_roles(UserRole.superadmin))]
SchoolAdminUser = Annotated[User, Depends(require_roles(UserRole.school_admin))]


def role_in(user: User, roles: Iterable[UserRole]) -> bool:
    return user.role in set(roles)


def assert_tenant(user: User) -> uuid.UUID:
    """Returns tenant_id or 403 — superadmin cannot use tenant-scoped endpoints directly."""
    if user.tenant_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="superadmin must use /admin endpoints",
        )
    return user.tenant_id


def assert_can_access_student(user: User, student_id: uuid.UUID) -> None:
    """Teachers see everyone in their tenant; student users only see their own
    backing Student row. Parent access is enforced at route level via the
    ParentStudent junction (see students.py)."""
    if user.role is UserRole.teacher:
        return
    linked = getattr(user, "_student_id", None)
    if linked == student_id:
        return
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="not permitted to access this student's data",
    )
