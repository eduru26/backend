from __future__ import annotations

from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

_pool: ArqRedis | None = None

# Function-name → queue routing. Both workers live on the same Redis DB
# (so we can share connections / dashboards), but they listen on
# distinct queues so the main worker never picks up an ingest job it
# can't execute, and vice versa. Default queue is `arq:queue`; the
# ingest worker uses `arq:queue:ingest`.
INGEST_QUEUE = "arq:queue:ingest"
_INGEST_FUNCS = frozenset({"ingest_textbook_job"})


def arq_redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(get_settings().redis_arq_url)


async def get_arq() -> ArqRedis:
    """Process-wide arq connection pool for *enqueuing* jobs.

    Used by API routes. The worker process has its own pool managed by
    arq's `WorkerSettings`.
    """
    global _pool
    if _pool is None:
        _pool = await create_pool(arq_redis_settings())
    return _pool


async def close_arq() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue(func_name: str, *args: Any, **kwargs: Any) -> str | None:
    """Submit a job to the worker. Returns the arq job id.

    Degrades gracefully: if Redis is unreachable we log and return None
    rather than failing the originating request. Callers that need the
    work to be guaranteed should check the return value.
    """
    try:
        pool = await get_arq()
        # Route ingest jobs onto the dedicated queue so the main worker
        # never picks one up and silently drops it as an unknown
        # function. All other jobs use arq's default queue.
        if func_name in _INGEST_FUNCS:
            job = await pool.enqueue_job(
                func_name, *args, _queue_name=INGEST_QUEUE, **kwargs,
            )
        else:
            job = await pool.enqueue_job(func_name, *args, **kwargs)
        if job is not None:
            logger.info("queue.enqueued", func=func_name, job_id=job.job_id)
            return job.job_id
        return None
    except Exception as exc:  # noqa: BLE001 — never fail the request on queue error
        logger.error("queue.enqueue_failed", func=func_name, error=str(exc))
        return None


async def job_status(job_id: str) -> dict[str, Any]:
    """Return {status, result?} for a previously enqueued job."""
    from arq.jobs import Job

    try:
        pool = await get_arq()
        job = Job(job_id, redis=pool)
        status = await job.status()
        out: dict[str, Any] = {"job_id": job_id, "status": status.value}
        if status.value == "complete":
            try:
                out["result"] = await job.result(timeout=1)
            except Exception:  # noqa: BLE001 — result may have expired
                out["result"] = None
        return out
    except Exception as exc:  # noqa: BLE001
        return {"job_id": job_id, "status": "unknown", "error": str(exc)}


async def queue_depth() -> int:
    """Number of jobs waiting in the default arq queue — a scaling signal."""
    try:
        pool = await get_arq()
        return int(await pool.llen("arq:queue"))
    except Exception:  # noqa: BLE001
        return 0
