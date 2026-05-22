from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Student, User, UserRole


def normalize_phone(phone: str | None) -> str | None:
    """Return digit-only form of a phone string, or None if empty.
    Used to populate users.phone_digits so lookups can hit a regular B-tree
    index regardless of how the input was formatted ('+91 999 000 0333',
    '+919990000333', '99900-00333', ' 9990000333 ', etc.).
    """
    if not phone:
        return None
    digits = ''.join(c for c in phone if c.isdigit())
    return digits or None


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_email(self, email: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.email == email.lower())
        )
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> User | None:
        result = await self.session.execute(
            select(User).where(User.phone == phone.strip())
        )
        return result.scalar_one_or_none()

    async def get_by_handle(self, handle: str) -> User | None:
        """O(log n) lookup of a student user by their roll-number handle.

        Hits the partial unique index `uq_users_handle`. Trim+exact match —
        no normalization (roll numbers are typically uppercase alnum like
        STU001 and we want the casing to be deterministic).
        """
        cleaned = handle.strip()
        if not cleaned:
            return None
        result = await self.session.execute(
            select(User).where(User.handle == cleaned).limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_phone_digits(self, digits: str) -> User | None:
        """O(1) lookup of an active user by the trailing 10 digits of their phone.

        Matches against the indexed `phone_digits` column, which holds the
        digit-only normalized form. Accepts any formatting variant; the
        caller is expected to pass the user's typed value (we strip
        non-digits here too as a safety net).
        """
        cleaned = ''.join(c for c in digits if c.isdigit())
        if len(cleaned) < 10:
            return None
        tail = cleaned[-10:]
        # `phone_digits` is e.g. '919990000333' or '9990000333'; either way
        # the last 10 digits are the household phone. A LIKE '%<tail>' lets
        # PG plan with the trailing-match efficiently when paired with a
        # trgm index, but a plain LIKE keeps the index optional. For our
        # data sizes the index scan is fine.
        result = await self.session.execute(
            select(User).where(
                User.is_active.is_(True),
                User.phone_digits.isnot(None),
                User.phone_digits.like(f"%{tail}"),
            ).limit(1)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        email: str | None = None,
        phone: str | None = None,
        password_hash: str,
        role: UserRole,
        full_name: str | None = None,
    ) -> User:
        user = User(
            email=email.lower() if email else None,
            phone=phone,
            phone_digits=normalize_phone(phone),
            password_hash=password_hash,
            role=role,
            full_name=full_name,
        )
        self.session.add(user)
        await self.session.flush()
        await self.session.refresh(user)
        return user


class HandleAlreadyTaken(Exception):
    """Raised when a student's roll number is already in use as a login handle.

    Callers (onboarding endpoints) should translate this into a 409 with a
    helpful message — the global partial unique index on users.handle means
    two students cannot share a handle, even across tenants. The school
    admin needs to pick a different roll number for the new student.
    """


async def ensure_student_user(
    session: AsyncSession,
    *,
    student: Student,
    password_hash: str,
) -> User:
    """Create the dedicated User row a student logs in with, and link it.

    Idempotent in two ways:
      • If `student.user_id` already points to a valid User, return it.
      • If a User with the same (tenant_id, handle, role=student) already
        exists (e.g. created by an earlier migration backfill), reuse it
        and just wire `student.user_id` to that row.

    Raises `HandleAlreadyTaken` if the handle is in use by a *different*
    user (different tenant, different role) and we therefore cannot insert.
    Caller should surface a 409 to the school admin / teacher.

    The DB trigger `trg_assert_student_role` enforces role='student' on
    students.user_id at insert/update time; this helper keeps the app
    layer aligned with that invariant.
    """
    if student.user_id is not None:
        existing = await session.get(User, student.user_id)
        if existing is not None:
            return existing

    # Reuse an existing student User created by a prior backfill / re-run.
    reuse = await session.scalar(
        select(User).where(
            User.tenant_id == student.tenant_id,
            User.handle == student.handle,
            User.role == UserRole.student,
        )
    )
    if reuse is not None:
        student.user_id = reuse.id
        await session.flush()
        return reuse

    # Someone else (different tenant or different role) owns this handle —
    # the global partial unique index will reject our insert. Fail fast with
    # a typed error the caller can turn into a 409.
    conflict = await session.scalar(
        select(User.id).where(User.handle == student.handle)
    )
    if conflict is not None:
        raise HandleAlreadyTaken(
            f"Roll number '{student.handle}' is already in use as a login "
            "handle. Pick a different roll number for this student."
        )

    user = User(
        id=uuid.uuid4(),
        tenant_id=student.tenant_id,
        handle=student.handle,
        password_hash=password_hash,
        role=UserRole.student,
        full_name=student.full_name,
    )
    session.add(user)
    await session.flush()
    student.user_id = user.id
    await session.flush()
    return user
