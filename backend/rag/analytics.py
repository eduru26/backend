from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.logging import get_logger
from backend.db.models import RagQueryEvent

logger = get_logger(__name__)


@dataclass(frozen=True)
class TrendingIntent:
    intent: str
    count: int


class QueryAnalytics:
    """Append-only telemetry over chat turns + simple trend aggregates.

    The chat pipeline calls `record(...)` for every turn (allowed or
    blocked). The proactive-suggestions endpoint calls `trending_intents`
    to surface what users are asking most. We never store the raw
    question text here — the `messages` table already has it; we keep a
    sha256 fingerprint so we can detect "the same question is being
    asked by many users" without duplicating storage.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        role: str,
        intent: str,
        question: str,
        blocked: bool,
        block_reason: str | None,
        latency_ms: int | None,
        rag_hits: int,
    ) -> None:
        evt = RagQueryEvent(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            user_id=user_id,
            role=role,
            intent=intent,
            blocked=blocked,
            block_reason=(block_reason or None) and block_reason[:200],
            question_hash=hashlib.sha256(question.encode("utf-8")).hexdigest(),
            latency_ms=latency_ms,
            rag_hits=rag_hits,
        )
        self._session.add(evt)
        # Do not flush here — the chat endpoint owns the commit lifecycle.

    async def trending_intents(
        self,
        *,
        tenant_id: uuid.UUID,
        role: str | None = None,
        days: int = 7,
        limit: int = 5,
    ) -> list[TrendingIntent]:
        since = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(RagQueryEvent.intent, func.count().label("n"))
            .where(
                RagQueryEvent.tenant_id == tenant_id,
                RagQueryEvent.created_at >= since,
                RagQueryEvent.blocked.is_(False),
            )
            .group_by(RagQueryEvent.intent)
            .order_by(func.count().desc())
            .limit(limit)
        )
        if role:
            stmt = stmt.where(RagQueryEvent.role == role)
        rows = await self._session.execute(stmt)
        return [TrendingIntent(intent=r.intent, count=int(r.n)) for r in rows.all()]
