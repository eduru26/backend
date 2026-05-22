from __future__ import annotations

import time
from collections import deque
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class MetricsState:
    """Process-local request metrics for the autoscaler + /metrics.

    Deliberately tiny and in-process — the autoscaler aggregates across
    replicas by hitting each one (or the load balancer) and summing. We
    keep a rolling window of recent request durations so we can expose
    p95 latency and a short-term request rate, which are better scaling
    signals than instantaneous in-flight count alone.
    """

    def __init__(self, window: int = 200) -> None:
        self.in_flight = 0
        self.total_requests = 0
        self.total_errors = 0
        self._durations: deque[float] = deque(maxlen=window)
        self._timestamps: deque[float] = deque(maxlen=window)

    def record(self, duration_ms: float, is_error: bool) -> None:
        self.total_requests += 1
        if is_error:
            self.total_errors += 1
        self._durations.append(duration_ms)
        self._timestamps.append(time.time())

    def snapshot(self) -> dict[str, Any]:
        durations = sorted(self._durations)
        p95 = durations[int(len(durations) * 0.95)] if durations else 0.0
        # requests in the last 60s → a rate the autoscaler can threshold
        cutoff = time.time() - 60
        recent = sum(1 for t in self._timestamps if t >= cutoff)
        return {
            "in_flight": self.in_flight,
            "total_requests": self.total_requests,
            "total_errors": self.total_errors,
            "requests_last_60s": recent,
            "p95_latency_ms": round(p95, 1),
        }


METRICS = MetricsState()


class MetricsMiddleware:
    """Pure-ASGI middleware tracking in-flight count + latency.

    ASGI (not BaseHTTPMiddleware) so it adds no measurable overhead and
    doesn't interfere with streaming responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        METRICS.in_flight += 1
        started = time.perf_counter()
        status_code = 500

        async def _send(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self._app(scope, receive, _send)
        finally:
            METRICS.in_flight -= 1
            elapsed_ms = (time.perf_counter() - started) * 1000
            METRICS.record(elapsed_ms, is_error=status_code >= 500)
