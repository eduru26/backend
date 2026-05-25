"""arq settings for the dedicated textbook-ingest worker.

Kept separate from `backend.worker.settings.WorkerSettings` because
docling pulls in 2-3 GB of layout / OCR models on first run; we don't
want that footprint on every replica of the chat / paper-gen worker.

Run with:  arq backend.worker.ingest_settings.IngestWorkerSettings
"""
from __future__ import annotations

from backend.core.config import get_settings
from backend.core.logging import configure_logging, get_logger
from backend.worker.jobs import ingest_textbook_job
from backend.worker.queue import INGEST_QUEUE, arq_redis_settings

logger = get_logger(__name__)


async def _on_startup(ctx: dict) -> None:
    """Boot the ingest worker and verify docling is importable.

    Importing docling at startup (rather than lazily inside the job)
    means a broken install fails the container immediately instead of
    failing every uploaded PDF later. The trade-off is a slower first
    boot — docling pulls in transformers + torch + onnxruntime — but
    we'd rather pay that once than debug "ingest stuck on Queued".
    """
    configure_logging()
    try:
        from docling.document_converter import DocumentConverter  # noqa: F401
        logger.info("ingest_worker.docling_ready")
    except Exception as exc:
        logger.exception("ingest_worker.docling_import_failed",
                         error=str(exc))
        raise
    logger.info("ingest_worker.startup", queue=INGEST_QUEUE)


async def _on_shutdown(ctx: dict) -> None:
    from backend.db.base import dispose_engine
    await dispose_engine()
    logger.info("ingest_worker.shutdown")


class IngestWorkerSettings:
    """Only registers the docling ingest job — nothing else.

    `max_jobs=1` because docling is CPU/memory heavy and we'd rather
    queue PDFs than thrash. `job_timeout=30m` because a 200-page Hindi
    textbook with heavy OCR pages can run that long on first execution
    while docling warms up its layout model.

    `queue_name` is the dedicated `arq:queue:ingest`. The api side's
    enqueue() routes ingest jobs onto this queue so the main worker
    (which doesn't know about docling) never picks one up.
    """

    functions = [ingest_textbook_job]
    queue_name = INGEST_QUEUE
    redis_settings = arq_redis_settings()
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_jobs = 1
    job_timeout = 60 * 30
    max_tries = 1   # docling failures are rarely transient; let the
                    # admin retry from the UI rather than auto-retry
                    # and silently consume the same broken upload.
    keep_result = 3600

    _ = get_settings  # keep import alive for env validation parity
