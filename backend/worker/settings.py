from __future__ import annotations

from backend.core.config import get_settings
from backend.core.logging import configure_logging, get_logger
from backend.worker.jobs import (
    generate_paper_job,
    ingest_document_job,
    lms_event_job,
    reindex_job,
)
from backend.worker.queue import arq_redis_settings

logger = get_logger(__name__)


async def _on_startup(ctx: dict) -> None:
    configure_logging()
    logger.info("worker.startup")


async def _on_shutdown(ctx: dict) -> None:
    from backend.db.base import dispose_engine
    await dispose_engine()
    logger.info("worker.shutdown")


class WorkerSettings:
    """arq worker entrypoint.

    Run with:  arq backend.worker.settings.WorkerSettings

    `max_jobs` bounds concurrency so the worker doesn't open more DB /
    Ollama connections than the pool allows. Jobs that raise are retried
    by arq up to `max_tries`; embedding/ingest jobs are idempotent
    (sha256 dedup) so retries are safe.
    """

    functions = [
        ingest_document_job, reindex_job, lms_event_job, generate_paper_job,
    ]
    redis_settings = arq_redis_settings()
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_jobs = get_settings().worker_max_jobs
    # 5 min — generate_paper_job is now the slowest job (per-topic LLM
    # calls). Textbook ingestion is no longer worker work (pack import is
    # synchronous in the request handler).
    job_timeout = 300
    max_tries = 3
    keep_result = 3600         # keep job results 1h for status polling
