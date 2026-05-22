"""Notifications inbox — per-user, mark-read endpoints."""
from __future__ import annotations

import uuid
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select, update

from backend.api.deps import CurrentUser, SessionDep
from backend.db.models import Notification

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: str
    kind: str
    title: str
    body: str
    link: str | None
    payload: dict | None
    read_at: datetime | None
    created_at: datetime


class UnreadCountOut(BaseModel):
    unread: int


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    user: CurrentUser,
    session: SessionDep,
    only_unread: bool = False,
    limit: int = 50,
) -> list[NotificationOut]:
    stmt = select(Notification).where(Notification.user_id == user.id)
    if only_unread:
        stmt = stmt.where(Notification.read_at.is_(None))
    stmt = stmt.order_by(Notification.created_at.desc()).limit(limit)
    rows = (await session.execute(stmt)).scalars().all()
    return [
        NotificationOut(
            id=str(n.id),
            kind=n.kind.value,
            title=n.title,
            body=n.body,
            link=n.link,
            payload=n.payload,
            read_at=n.read_at,
            created_at=n.created_at,
        )
        for n in rows
    ]


@router.get("/unread-count", response_model=UnreadCountOut)
async def unread_count(user: CurrentUser, session: SessionDep) -> UnreadCountOut:
    stmt = select(func.count(Notification.id)).where(
        Notification.user_id == user.id,
        Notification.read_at.is_(None),
    )
    n = (await session.execute(stmt)).scalar_one()
    return UnreadCountOut(unread=int(n or 0))


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    notification_id: str,
    user: CurrentUser,
    session: SessionDep,
) -> None:
    nid = uuid.UUID(notification_id)
    n = await session.get(Notification, nid)
    if not n or n.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "not found")
    if n.read_at is None:
        n.read_at = datetime.utcnow()


@router.post("/read-all", status_code=status.HTTP_204_NO_CONTENT)
async def mark_all_read(user: CurrentUser, session: SessionDep) -> None:
    await session.execute(
        update(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
        .values(read_at=datetime.utcnow())
    )
