"""FastAPI router that receives events from the Frappe ``ai_mentor`` app.

Wire format (set by ``ai_mentor.mentor.bridge`` on the Frappe side):

    POST /lms/events/<event_slug>
    Authorization: Bearer <LMS_INBOUND_SECRET>
    X-Tenant-Code: <tenant>
    Content-Type: application/json

    {
      "event": "<event_slug>",
      "site":  "<frappe-site-name>",
      "ts":    "<iso-8601>",
      "payload": { ... event-specific ... }
    }

The event slug is the URL path component AND the ``event`` field — both are
checked. Handlers run as background tasks so the inbound request returns
immediately; on misconfiguration the bridge skips the callback gracefully
(see :class:`backend.lms.client.LMSCallbackClient`).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel

from backend.core.logging import get_logger
from backend.lms.auth import verify_lms_bearer
from backend.lms.handlers import EVENT_DISPATCH
from backend.worker.queue import enqueue

logger = get_logger(__name__)

router = APIRouter(prefix="/lms/events", tags=["lms"])


class _Envelope(BaseModel):
    event: str | None = None
    site: str | None = None
    ts: str | None = None
    payload: dict[str, Any]


@router.post("/{event_slug}", status_code=status.HTTP_202_ACCEPTED)
async def receive_event(
    event_slug: str,
    envelope: _Envelope,
    background: BackgroundTasks,
    tenant: Annotated[str, Depends(verify_lms_bearer)],
) -> dict[str, Any]:
    if event_slug not in EVENT_DISPATCH:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"unknown event '{event_slug}'",
        )

    if envelope.event and envelope.event != event_slug:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"event mismatch: url='{event_slug}' body='{envelope.event}'",
        )

    payload_cls, _handler = EVENT_DISPATCH[event_slug]
    try:
        # Validate the payload shape synchronously so a malformed event is
        # rejected with 422 at the boundary — but discard the typed object;
        # the worker re-builds it. We pass the plain dict over the queue.
        payload_cls(**envelope.payload)
    except Exception as exc:  # pydantic validation
        logger.warning(
            "lms.event.invalid_payload",
            event=event_slug,
            tenant=tenant,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid payload for {event_slug}: {exc}",
        ) from exc

    # Durable hand-off: the handler runs in the arq worker, so it survives
    # a backend restart and is retried on failure. If the queue is down we
    # fall back to an in-process BackgroundTask so events aren't lost.
    job_id = await enqueue(
        "lms_event_job", event_slug=event_slug, payload=envelope.payload
    )
    if job_id is None:
        logger.warning("lms.event.queue_down_fallback", event=event_slug)

        async def _run() -> None:
            try:
                await _handler(payload_cls(**envelope.payload))
            except Exception:
                logger.exception("lms.event.handler_failed", event=event_slug)

        background.add_task(_run)

    logger.info(
        "lms.event.received",
        event=event_slug,
        tenant=tenant,
        site=envelope.site,
        job_id=job_id,
    )
    return {"accepted": True, "event": event_slug, "tenant": tenant, "job_id": job_id}
