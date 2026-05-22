from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Conversation, Message, MessageRole


@dataclass(frozen=True)
class MemoryTurn:
    role: str       # "user" | "assistant"
    content: str


@dataclass(frozen=True)
class MemoryContext:
    """Last-N turns from the conversation, ready to splice into the prompt.

    `turns` excludes the message currently being processed — the
    PromptAgent will append the live question itself. We trim long bodies
    aggressively because Ollama context windows are small and these are
    only meant to provide *recency*, not full transcript.
    """

    turns: tuple[MemoryTurn, ...]

    def to_prompt_block(self) -> str:
        if not self.turns:
            return ""
        lines = ["RECENT CONVERSATION:"]
        for t in self.turns:
            who = "User" if t.role == "user" else "Mentor"
            lines.append(f"  {who}: {t.content}")
        return "\n".join(lines)


class SessionManager:
    """Reads the last-N messages of a conversation for prompt memory.

    The chat already persists every turn in `messages`; the session
    manager just loads a bounded window. We exclude the very latest user
    message because it's about to be replayed as the live question — we
    don't want to duplicate it in the memory block.
    """

    DEFAULT_TURNS = 6
    MAX_CONTENT_CHARS = 400

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def load(
        self,
        conversation_id: str | None,
        *,
        exclude_latest_user: bool = True,
        turns: int = DEFAULT_TURNS,
    ) -> MemoryContext:
        if not conversation_id:
            return MemoryContext(turns=())
        try:
            cid = uuid.UUID(conversation_id)
        except ValueError:
            return MemoryContext(turns=())

        # Quick existence check — the conversation may have been deleted
        # mid-request; degrade silently.
        conv = await self._session.get(Conversation, cid)
        if conv is None:
            return MemoryContext(turns=())

        # Fetch the most recent (2 * turns) messages then chronologically
        # order. 2x because a turn = user+assistant pair, and we may drop
        # the trailing user.
        limit = max(2, turns * 2)
        result = await self._session.execute(
            select(Message)
            .where(Message.conversation_id == cid)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        rows = list(reversed(result.scalars().all()))
        if not rows:
            return MemoryContext(turns=())

        if exclude_latest_user and rows[-1].role is MessageRole.user:
            rows = rows[:-1]

        out: list[MemoryTurn] = []
        for m in rows[-turns:]:
            body = (m.content or "").strip().replace("\n", " ")
            if len(body) > self.MAX_CONTENT_CHARS:
                body = body[: self.MAX_CONTENT_CHARS - 1] + "…"
            out.append(MemoryTurn(role=m.role.value, content=body))
        return MemoryContext(turns=tuple(out))
