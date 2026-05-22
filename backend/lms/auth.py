"""Static bearer-token auth for inbound LMS events.

Frappe's ``ai_mentor.mentor.bridge`` POSTs server-to-server to ``/lms/events/*``
with ``Authorization: Bearer <Tenant Profile.ai_backend_secret>``. We mirror
that secret as ``LMS_INBOUND_SECRET`` in this service's environment.

This is *not* the OAuth2 user-auth used elsewhere — there is no logged-in
user behind a Frappe doc-event. Constant-time comparison; no fallback to
query string or unauthenticated path.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from backend.core.config import Settings, get_settings


async def verify_lms_bearer(
    authorization: Annotated[str | None, Header()] = None,
    x_tenant_code: Annotated[str | None, Header()] = None,
    settings: Annotated[Settings, Depends(get_settings)] = None,  # type: ignore[assignment]
) -> str:
    """Returns the verified tenant code (or empty string if not configured).

    Raises 401 on missing/wrong bearer; raises 400 if the header tenant
    code disagrees with the configured one (catches DNS misroutes).
    """
    if not settings.lms_inbound_secret:
        # Service started without the bridge configured. Fail closed —
        # better to 503 than to silently accept anonymous events.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LMS bridge inbound secret is not configured",
        )

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    presented = authorization[len("Bearer ") :].strip()
    if not hmac.compare_digest(settings.lms_inbound_secret, presented):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if settings.lms_tenant_code and x_tenant_code and x_tenant_code != settings.lms_tenant_code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"event addressed to tenant '{x_tenant_code}' but service is configured for '{settings.lms_tenant_code}'",
        )

    return x_tenant_code or settings.lms_tenant_code
