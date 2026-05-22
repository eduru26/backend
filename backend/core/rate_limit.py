from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)


def _client_key(request: Request) -> str:
    """Rate-limit key = real client IP.

    Behind the nginx gateway the socket peer is nginx, so the true
    client IP arrives in `X-Forwarded-For` (nginx is configured to set
    it). We take the first hop. Falls back to the socket address when
    the header is absent (e.g. direct local calls / tests).
    """
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return get_remote_address(request)


def _build_limiter() -> Limiter:
    s = get_settings()
    # Redis-backed storage so limits are shared across every backend
    # replica — a per-process in-memory counter would let a client get
    # N× the limit by spreading calls across N replicas. If Redis is
    # unreachable at startup we fall back to in-memory so the app still
    # boots (degraded: limits become per-replica).
    storage = s.redis_ratelimit_url if s.rate_limit_enabled else "memory://"
    try:
        limiter = Limiter(
            key_func=_client_key,
            storage_uri=storage,
            default_limits=[s.rate_limit_default],
            enabled=s.rate_limit_enabled,
            headers_enabled=True,   # emit X-RateLimit-* response headers
        )
    except Exception as exc:  # noqa: BLE001 — never block boot on rate-limit infra
        logger.warning("ratelimit.storage.fallback", error=str(exc))
        limiter = Limiter(
            key_func=_client_key,
            storage_uri="memory://",
            default_limits=[s.rate_limit_default],
            enabled=s.rate_limit_enabled,
            headers_enabled=True,
        )
    return limiter


# Single shared limiter — imported by main.py (registers the handler +
# middleware) and by routers that need per-route limits.
limiter = _build_limiter()
