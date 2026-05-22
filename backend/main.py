from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from backend.api.academic_year import router as academic_year_router
from backend.api.admin import router as admin_router
from backend.api.announcements import router as announcements_router
from backend.api.auth import router as auth_router
from backend.api.chat import router as chat_router
from backend.api.ingest import router as ingest_router
from backend.api.lms_events import router as lms_events_router
from backend.api.notifications import router as notifications_router
from backend.api.rag_documents import router as rag_documents_router
from backend.api.school_admin import router as school_admin_router
from backend.api.school_analytics import router as school_analytics_router
from backend.api.students import router as students_router
from backend.api.teacher_mgmt import router as teacher_mgmt_router
from backend.api.upload import router as upload_router
from backend.core.circuit_breaker import all_breakers
from backend.core.config import get_settings
from backend.core.logging import configure_logging, get_logger
from backend.core.metrics import METRICS, MetricsMiddleware
from backend.core.rate_limit import limiter
from backend.core.redis_client import close_redis, healthcheck as redis_healthcheck
from backend.db.base import dispose_engine, healthcheck
from backend.services import class_reminders
from backend.worker.queue import close_arq, queue_depth

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    logger.info("app.startup", env=settings.app_env, ollama_model=settings.ollama_gen_model)
    tasks: dict = {}
    class_reminders.start(tasks)
    yield
    await class_reminders.stop(tasks)
    await dispose_engine()
    await close_redis()
    await close_arq()
    logger.info("app.shutdown")


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Eduru",
        version="0.4.0",
        description="Chat-first academic mentoring system — Student, Parent, Teacher, School Admin.",
        lifespan=lifespan,
    )

    # ── Rate limiting (slowapi + Redis) ─────────────────────
    # The limiter is shared across replicas via Redis so a client can't
    # multiply its quota by hitting different backends. SlowAPIMiddleware
    # applies the catch-all default; routes add stricter @limiter.limit.
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # ── Metrics (in-flight + latency) — feeds the autoscaler ─
    app.add_middleware(MetricsMiddleware)

    # ── CORS ────────────────────────────────────────────────
    # The Next.js apps proxy /api/* server-side, so browser calls are
    # same-origin and CORS is rarely exercised. Auth is Bearer-token
    # (no cookies), so credentials are not allowed — which also makes
    # the previous `*` + allow_credentials=True (an invalid, browser-
    # rejected combination) correct.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Health & metrics endpoints ──────────────────────────

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """Liveness — cheap, no downstream calls. Used by the LB/orchestrator."""
        return {"status": "ok"}

    @app.get("/health/db", tags=["health"])
    async def health_db() -> dict[str, object]:
        return await healthcheck()

    @app.get("/health/full", tags=["health"])
    async def health_full() -> dict[str, object]:
        """Readiness — db + redis + circuit-breaker states."""
        return {
            "db": await healthcheck(),
            "redis": await redis_healthcheck(),
            "breakers": all_breakers(),
        }

    @app.get("/metrics", tags=["health"])
    async def metrics() -> dict[str, object]:
        """Operational metrics consumed by the autoscaler.

        Per-replica numbers — the autoscaler hits each replica (or the
        LB) and aggregates. `queue_depth` is global (read from Redis).
        """
        return {
            **METRICS.snapshot(),
            "queue_depth": await queue_depth(),
            "breakers": all_breakers(),
        }

    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(school_admin_router)
    app.include_router(school_analytics_router)
    app.include_router(academic_year_router)
    app.include_router(teacher_mgmt_router)
    app.include_router(ingest_router)
    app.include_router(upload_router)
    app.include_router(rag_documents_router)
    app.include_router(chat_router)
    app.include_router(students_router)
    app.include_router(announcements_router)
    app.include_router(notifications_router)
    app.include_router(lms_events_router)

    return app


app = create_app()
