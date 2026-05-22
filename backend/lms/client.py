"""Async HTTP callback client → Frappe ``ai_mentor.mentor.webhook.*``.

Single instance per process via :func:`get_client`. Keeps a reusable
``httpx.AsyncClient`` so we don't pay TLS/connection setup per call.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from backend.core.circuit_breaker import get_breaker
from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.lms.models import (
    ConversationMessageOut,
    InsightOut,
    QuestionTagOut,
    RiskScoreOut,
)

logger = get_logger(__name__)


class LMSCallbackClient:
    def __init__(self) -> None:
        settings = get_settings()
        self._base_url = settings.lms_base_url.rstrip("/")
        self._secret = settings.lms_outbound_secret
        self._timeout = settings.lms_callback_timeout_sec
        self._max_retries = settings.lms_callback_max_retries
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        base_url=self._base_url,
                        timeout=self._timeout,
                        headers={
                            "authorization": f"Bearer {self._secret}",
                            "content-type": "application/json",
                            "accept": "application/json",
                        },
                    )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --------------------------------------------------------------------- #
    async def ingest_insights(self, insights: list[InsightOut]) -> dict[str, Any]:
        return await self._post(
            "/api/method/ai_mentor.mentor.webhook.ingest_insights",
            {"insights": [i.model_dump() for i in insights]},
        )

    async def ingest_question_tags(self, tags: list[QuestionTagOut]) -> dict[str, Any]:
        return await self._post(
            "/api/method/ai_mentor.mentor.webhook.ingest_question_tags",
            {"tags": [t.model_dump() for t in tags]},
        )

    async def ingest_risk_scores(self, rows: list[RiskScoreOut]) -> dict[str, Any]:
        return await self._post(
            "/api/method/ai_mentor.mentor.webhook.ingest_risk_scores",
            {"rows": [r.model_dump() for r in rows]},
        )

    async def append_message(self, message: ConversationMessageOut) -> dict[str, Any]:
        return await self._post(
            "/api/method/ai_mentor.mentor.webhook.append_message",
            message.model_dump(),
        )

    # --------------------------------------------------------------------- #
    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not self._base_url or not self._secret:
            logger.warning("lms.callback.skipped", reason="bridge_unconfigured", path=path)
            return {"ok": False, "skipped": True}

        # Breaker wraps the retry loop: transient blips are absorbed by the
        # retries, a sustained Frappe outage trips the breaker so callbacks
        # fail fast instead of every worker job burning its retry budget.
        return await get_breaker("lms_callback").call(self._post_impl, path, body)

    async def _post_impl(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        client = await self._get_client()
        last_exc: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = await client.post(path, json=body)
                resp.raise_for_status()
                return resp.json().get("message", {})
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning(
                    "lms.callback.retry",
                    path=path,
                    attempt=attempt,
                    error=str(exc),
                )
                await asyncio.sleep(min(2**attempt, 10))
        logger.error("lms.callback.failed", path=path, error=str(last_exc))
        raise RuntimeError(f"LMS callback failed after {self._max_retries} attempts: {last_exc}")


_client: LMSCallbackClient | None = None


def get_client() -> LMSCallbackClient:
    global _client
    if _client is None:
        _client = LMSCallbackClient()
    return _client


async def dispose_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
