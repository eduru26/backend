from __future__ import annotations

import hashlib
import json
from typing import Any

import redis.asyncio as aioredis

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

_pool: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Process-wide async Redis client.

    `redis.asyncio` manages its own connection pool internally, so a
    single client object is the right granularity — one per process,
    shared across all requests and the arq worker.
    """
    global _pool
    if _pool is None:
        url = get_settings().redis_url
        _pool = aioredis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2.0,
            socket_timeout=2.0,
            health_check_interval=30,
        )
    return _pool


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


# ── cache helpers ──────────────────────────────────────────
#
# Every helper degrades gracefully: if Redis is unreachable the call
# falls through as a cache miss. Caching must never be load-bearing for
# correctness — it's a latency/cost optimisation only.


def cache_key(*parts: Any) -> str:
    """Deterministic, collision-resistant key from arbitrary parts."""
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    return f"eduru:cache:{digest}"


async def cache_get_json(key: str) -> Any | None:
    if not get_settings().cache_enabled:
        return None
    try:
        raw = await get_redis().get(key)
    except Exception as exc:  # noqa: BLE001 — degrade to miss
        logger.warning("cache.get.failed", error=str(exc))
        return None
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


async def cache_set_json(key: str, value: Any, ttl_sec: int) -> None:
    if not get_settings().cache_enabled:
        return
    try:
        await get_redis().set(key, json.dumps(value), ex=ttl_sec)
    except Exception as exc:  # noqa: BLE001 — cache write is best-effort
        logger.warning("cache.set.failed", error=str(exc))


async def cache_get_vector(key: str) -> list[float] | None:
    val = await cache_get_json(key)
    if isinstance(val, list) and all(isinstance(x, (int, float)) for x in val):
        return [float(x) for x in val]
    return None


async def healthcheck() -> dict[str, Any]:
    try:
        pong = await get_redis().ping()
        return {"redis": "ok" if pong else "unexpected"}
    except Exception as exc:  # noqa: BLE001
        return {"redis": "error", "detail": str(exc)}
