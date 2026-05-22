"""Super-admin endpoints — manage schools (tenants) and their users."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, EmailStr
from sqlalchemy import delete as sql_delete, select

from backend.api.deps import CurrentUser, SessionDep, require_roles
from backend.core.security import hash_password
from backend.db.models import (
    Announcement, AnnouncementAudience, Classroom, Notification, NotificationKind,
    Tenant, TenantPlan, User, UserRole,
)
from backend.db.repositories.tenants import TenantRepository
from backend.db.repositories.users import UserRepository

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_roles(UserRole.superadmin))],
)


# ── Schemas ──────────────────────────────────────────────


# A 500 KB image encoded as a base64 data URI is ~684 KB of text.
MAX_LOGO_CHARS = 700_000


def _validate_logo(logo: str | None) -> None:
    if logo is None or logo == "":
        return
    if not logo.startswith("data:image/"):
        raise HTTPException(status_code=400, detail="logo must be an image data URI")
    if len(logo) > MAX_LOGO_CHARS:
        raise HTTPException(status_code=400, detail="logo image must be 500 KB or smaller")


class TenantIn(BaseModel):
    name: str
    slug: str
    plan: TenantPlan = TenantPlan.starter
    logo_url: str | None = None


class TenantPatch(BaseModel):
    name: str | None = None
    is_active: bool | None = None
    plan: TenantPlan | None = None
    logo_url: str | None = None


class TenantOut(BaseModel):
    id: str
    name: str
    slug: str
    is_active: bool
    plan: TenantPlan
    logo_url: str | None = None
    created_at: datetime
    student_count: int = 0
    user_count: int = 0


class UserInTenant(BaseModel):
    email: EmailStr
    password: str
    full_name: str | None = None
    role: UserRole = UserRole.school_admin   # default to school_admin for new school users


class UserPatch(BaseModel):
    full_name: str | None = None
    role: UserRole | None = None
    is_active: bool | None = None
    password: str | None = None             # optional password reset


class UserOut(BaseModel):
    id: str
    email: str | None
    phone: str | None = None
    full_name: str | None
    role: UserRole
    is_active: bool
    created_at: datetime


class StatsOut(BaseModel):
    students: int
    users: int


# ── Tenant CRUD ──────────────────────────────────────────


@router.get("/tenants", response_model=list[TenantOut])
async def list_tenants(session: SessionDep) -> list[TenantOut]:
    repo = TenantRepository(session)
    tenants = await repo.list_all()
    out = []
    for t in tenants:
        s = await repo.stats(t.id)
        out.append(TenantOut(
            id=str(t.id), name=t.name, slug=t.slug,
            is_active=t.is_active, plan=t.plan, logo_url=t.logo_url,
            created_at=t.created_at,
            student_count=s["students"], user_count=s["users"],
        ))
    return out


@router.post("/tenants", response_model=TenantOut, status_code=status.HTTP_201_CREATED)
async def create_tenant(body: TenantIn, session: SessionDep) -> TenantOut:
    repo = TenantRepository(session)
    existing = await repo.get_by_slug(body.slug)
    if existing:
        raise HTTPException(status_code=409, detail=f"slug '{body.slug}' already in use")
    _validate_logo(body.logo_url)
    tenant = await repo.create(name=body.name, slug=body.slug, plan=body.plan)
    if body.logo_url:
        tenant.logo_url = body.logo_url
        await session.flush()
    await _provision_default_classrooms(tenant.id, session)
    return TenantOut(
        id=str(tenant.id), name=tenant.name, slug=tenant.slug,
        is_active=tenant.is_active, plan=tenant.plan, logo_url=tenant.logo_url,
        created_at=tenant.created_at,
    )


@router.get("/tenants/{tenant_id}", response_model=TenantOut)
async def get_tenant(tenant_id: str, session: SessionDep) -> TenantOut:
    tenant = await _must_get_tenant(tenant_id, session)
    repo = TenantRepository(session)
    s = await repo.stats(tenant.id)
    return TenantOut(
        id=str(tenant.id), name=tenant.name, slug=tenant.slug,
        is_active=tenant.is_active, plan=tenant.plan, logo_url=tenant.logo_url,
        created_at=tenant.created_at,
        student_count=s["students"], user_count=s["users"],
    )


@router.delete("/tenants/{tenant_id}", status_code=200)
async def delete_tenant(tenant_id: str, session: SessionDep) -> dict:
    from sqlalchemy.exc import IntegrityError
    tenant = await _must_get_tenant(tenant_id, session)
    try:
        await session.execute(sql_delete(Tenant).where(Tenant.id == tenant.id))
        await session.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete: school has academic data linked to its users. "
                   "Deactivate the school instead.",
        )
    return {"deleted": True}


@router.patch("/tenants/{tenant_id}", response_model=TenantOut)
async def update_tenant(tenant_id: str, body: TenantPatch, session: SessionDep) -> TenantOut:
    tenant = await _must_get_tenant(tenant_id, session)
    repo = TenantRepository(session)
    _validate_logo(body.logo_url)
    updates = body.model_dump(exclude_none=True)
    if updates:
        tenant = await repo.update(tenant.id, **updates)  # type: ignore[assignment]
    s = await repo.stats(tenant.id)  # type: ignore[union-attr]
    return TenantOut(
        id=str(tenant.id), name=tenant.name, slug=tenant.slug,  # type: ignore[union-attr]
        is_active=tenant.is_active, plan=tenant.plan, logo_url=tenant.logo_url,
        created_at=tenant.created_at,
        student_count=s["students"], user_count=s["users"],
    )


@router.get("/tenants/{tenant_id}/stats", response_model=StatsOut)
async def tenant_stats(tenant_id: str, session: SessionDep) -> StatsOut:
    tenant = await _must_get_tenant(tenant_id, session)
    repo = TenantRepository(session)
    s = await repo.stats(tenant.id)
    return StatsOut(**s)


# ── Users within a tenant ────────────────────────────────


@router.get("/tenants/{tenant_id}/users", response_model=list[UserOut])
async def list_tenant_users(tenant_id: str, session: SessionDep) -> list[UserOut]:
    tenant = await _must_get_tenant(tenant_id, session)
    result = await session.execute(
        select(User).where(User.tenant_id == tenant.id).order_by(User.email)
    )
    users = result.scalars().all()
    return [
        UserOut(
            id=str(u.id), email=u.email, phone=u.phone, full_name=u.full_name,
            role=u.role, is_active=u.is_active, created_at=u.created_at,
        )
        for u in users
    ]


@router.post(
    "/tenants/{tenant_id}/users",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_tenant_user(
    tenant_id: str, body: UserInTenant, session: SessionDep
) -> UserOut:
    tenant = await _must_get_tenant(tenant_id, session)

    if body.role is UserRole.superadmin:
        raise HTTPException(status_code=400, detail="cannot create superadmin via this endpoint")

    repo = UserRepository(session)
    existing = await repo.get_by_email(body.email)
    if existing:
        raise HTTPException(status_code=409, detail="email already registered")

    user = User(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        email=body.email,
        password_hash=hash_password(body.password),
        role=body.role,
        full_name=body.full_name,
    )
    session.add(user)
    await session.flush()

    # Idempotently provision Grade 1-10 classrooms when a school_admin is created,
    # in case the tenant was seeded outside the API (e.g. seed_users.py).
    if body.role is UserRole.school_admin:
        from sqlalchemy import func
        existing_count = await session.scalar(
            select(func.count()).where(Classroom.tenant_id == tenant.id)
        )
        if not existing_count:
            await _provision_default_classrooms(tenant.id, session)

    await session.refresh(user)
    return UserOut(
        id=str(user.id), email=user.email, phone=user.phone, full_name=user.full_name,
        role=user.role, is_active=user.is_active, created_at=user.created_at,
    )


@router.patch("/tenants/{tenant_id}/users/{user_id}", response_model=UserOut)
async def update_tenant_user(
    tenant_id: str,
    user_id: str,
    body: UserPatch,
    session: SessionDep,
) -> UserOut:
    """Edit name / role / active-status / password for any user inside a school."""
    tenant = await _must_get_tenant(tenant_id, session)

    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid user id")

    result = await session.execute(
        select(User).where(User.id == uid, User.tenant_id == tenant.id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="user not found in this school")

    if body.full_name is not None:
        user.full_name = body.full_name
    if body.role is not None:
        if body.role is UserRole.superadmin:
            raise HTTPException(status_code=400, detail="cannot assign superadmin role here")
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.password:
        user.password_hash = hash_password(body.password)

    await session.flush()
    return UserOut(
        id=str(user.id), email=user.email, phone=user.phone, full_name=user.full_name,
        role=user.role, is_active=user.is_active, created_at=user.created_at,
    )


@router.delete("/tenants/{tenant_id}/users/{user_id}", status_code=200)
async def delete_tenant_user(
    tenant_id: str,
    user_id: str,
    session: SessionDep,
) -> dict:
    from sqlalchemy.exc import IntegrityError
    tenant = await _must_get_tenant(tenant_id, session)
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid user id")
    exists = await session.scalar(
        select(User.id).where(User.id == uid, User.tenant_id == tenant.id)
    )
    if not exists:
        raise HTTPException(status_code=404, detail="user not found in this school")
    try:
        await session.execute(sql_delete(User).where(User.id == uid))
        await session.flush()
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete: user has assessments or uploads linked to their account. "
                   "Deactivate the account instead.",
        )
    return {"deleted": True}


# ── Broadcast announcements (platform-wide) ───────────────


class BroadcastIn(BaseModel):
    title: str
    body: str
    pinned: bool = False
    tenant_ids: list[str] | None = None   # None / empty → all active tenants


class BroadcastResult(BaseModel):
    delivered: int                # number of tenants the broadcast was created in
    tenant_ids: list[str]
    broadcast_id: str


class BroadcastSummary(BaseModel):
    broadcast_id: str
    title: str
    body: str
    pinned: bool
    created_at: datetime
    delivered: int                # number of tenant copies still alive
    tenant_ids: list[str]


@router.post("/broadcast", response_model=BroadcastResult, status_code=201)
async def broadcast_announcement(
    body: BroadcastIn, user: CurrentUser, session: SessionDep
) -> BroadcastResult:
    """Post a school-wide announcement into every (or selected) tenant.
    The superadmin is recorded as author; each tenant gets its own
    Announcement row + notifications fan-out so the existing per-tenant
    list endpoint surfaces it normally.
    """
    if not body.title.strip() or not body.body.strip():
        raise HTTPException(400, "Title and body are required.")

    if body.tenant_ids:
        try:
            tids = [uuid.UUID(t) for t in body.tenant_ids]
        except ValueError:
            raise HTTPException(400, "invalid tenant id in tenant_ids")
        result = await session.execute(
            select(Tenant).where(Tenant.id.in_(tids), Tenant.is_active.is_(True))
        )
        tenants = result.scalars().all()
    else:
        result = await session.execute(
            select(Tenant).where(Tenant.is_active.is_(True))
        )
        tenants = result.scalars().all()

    if not tenants:
        raise HTTPException(404, "No active tenants matched.")

    broadcast_id = uuid.uuid4()
    created_ids: list[str] = []
    for t in tenants:
        a = Announcement(
            id=uuid.uuid4(),
            tenant_id=t.id,
            author_id=user.id,
            audience=AnnouncementAudience.school,
            classroom_id=None,
            title=body.title.strip(),
            body=body.body.strip(),
            pinned=body.pinned,
            broadcast_id=broadcast_id,
        )
        session.add(a)
        await session.flush()
        created_ids.append(str(t.id))

        # Fan out notifications to every active user in the tenant
        user_rows = await session.execute(
            select(User.id).where(User.tenant_id == t.id, User.is_active.is_(True))
        )
        for uid in user_rows.scalars().all():
            if uid == user.id:
                continue
            session.add(Notification(
                user_id=uid,
                kind=NotificationKind.announcement,
                title=a.title,
                body=a.body[:280],
                link=f"/announcements/{a.id}",
                payload={"announcement_id": str(a.id), "broadcast": True},
            ))

    return BroadcastResult(
        delivered=len(created_ids),
        tenant_ids=created_ids,
        broadcast_id=str(broadcast_id),
    )


@router.get("/broadcasts", response_model=list[BroadcastSummary])
async def list_broadcasts(
    session: SessionDep, limit: int = 50
) -> list[BroadcastSummary]:
    """List platform broadcasts (newest first), grouped by broadcast_id."""
    result = await session.execute(
        select(Announcement)
        .where(Announcement.broadcast_id.isnot(None))
        .order_by(Announcement.created_at.desc())
    )
    rows = result.scalars().all()

    grouped: dict[uuid.UUID, dict] = {}
    for r in rows:
        bid = r.broadcast_id
        if bid is None:
            continue
        if bid not in grouped:
            grouped[bid] = {
                "broadcast_id": str(bid),
                "title": r.title,
                "body": r.body,
                "pinned": r.pinned,
                "created_at": r.created_at,
                "tenant_ids": [],
            }
        grouped[bid]["tenant_ids"].append(str(r.tenant_id))

    summaries = list(grouped.values())
    summaries.sort(key=lambda g: g["created_at"], reverse=True)
    summaries = summaries[: max(1, min(limit, 200))]

    return [
        BroadcastSummary(
            broadcast_id=g["broadcast_id"],
            title=g["title"], body=g["body"], pinned=g["pinned"],
            created_at=g["created_at"],
            delivered=len(g["tenant_ids"]),
            tenant_ids=g["tenant_ids"],
        )
        for g in summaries
    ]


@router.delete("/broadcasts/{broadcast_id}", status_code=200)
async def delete_broadcast(broadcast_id: str, session: SessionDep) -> dict:
    """Delete every tenant copy of a broadcast plus the related notifications."""
    try:
        bid = uuid.UUID(broadcast_id)
    except ValueError:
        raise HTTPException(400, "invalid broadcast id")

    matches = await session.execute(
        select(Announcement.id).where(Announcement.broadcast_id == bid)
    )
    announcement_ids = list(matches.scalars().all())
    if not announcement_ids:
        raise HTTPException(404, "broadcast not found")

    # Tidy up fan-out notifications (each Announcement was fanned out separately
    # with its own announcement_id in the payload).
    for aid in announcement_ids:
        await session.execute(
            sql_delete(Notification).where(
                Notification.kind == NotificationKind.announcement,
                Notification.payload["announcement_id"].astext == str(aid),
            )
        )
    await session.execute(
        sql_delete(Announcement).where(Announcement.broadcast_id == bid)
    )
    return {"deleted": len(announcement_ids)}


# ── Helpers ──────────────────────────────────────────────


async def _provision_default_classrooms(
    tenant_id: uuid.UUID, session: object
) -> None:
    """Create Pre-KG/LKG/UKG + Grade 1-10, Section A classrooms for a new school."""
    from backend.api._grades import DEFAULT_GRADES, classroom_name
    for grade in DEFAULT_GRADES:
        cls = Classroom(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            name=classroom_name(grade, "A"),
            grade=grade,
            section="A",
            capacity=35,
            teacher_id=None,
        )
        session.add(cls)  # type: ignore[union-attr]
    await session.flush()  # type: ignore[union-attr]


async def _must_get_tenant(tenant_id: str, session: object) -> Tenant:
    from sqlalchemy.ext.asyncio import AsyncSession
    try:
        tid = uuid.UUID(tenant_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid tenant id")
    result = await session.execute(select(Tenant).where(Tenant.id == tid))  # type: ignore[union-attr]
    tenant = result.scalar_one_or_none()
    if not tenant:
        raise HTTPException(status_code=404, detail="school not found")
    return tenant
