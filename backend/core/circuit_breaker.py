from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any, TypeVar

from backend.core.config import get_settings
from backend.core.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class CircuitState(str, Enum):
    closed = "closed"        # normal — calls flow through
    open = "open"            # tripped — calls fail fast, no downstream hit
    half_open = "half_open"  # probing — limited trial calls allowed


class CircuitOpenError(RuntimeError):
    """Raised when a call is rejected because the breaker is OPEN.

    Subclasses RuntimeError so existing broad handlers (e.g. the chat
    endpoint's graceful-degradation block) still catch it.
    """

    def __init__(self, name: str) -> None:
        super().__init__(
            f"circuit '{name}' is open — downstream service is unhealthy, "
            "failing fast"
        )
        self.circuit = name


class CircuitBreaker:
    """Async circuit breaker for a single downstream dependency.

    Why: a slow or dead dependency (Ollama hung, converter down) should
    not tie up request workers waiting on timeouts. After
    `fail_threshold` consecutive failures the breaker trips OPEN and
    every subsequent call fails *immediately* with `CircuitOpenError`
    until `recovery_sec` elapses. It then goes HALF_OPEN and admits a
    few trial calls — if they succeed it closes, if any fails it
    re-opens.

    One breaker instance per dependency. They are registered in
    `_REGISTRY` so `/metrics` and `/health` can report their state.
    """

    def __init__(
        self,
        name: str,
        *,
        fail_threshold: int | None = None,
        recovery_sec: float | None = None,
        half_open_max: int | None = None,
        ignore: tuple[type[BaseException], ...] = (),
    ) -> None:
        s = get_settings()
        self.name = name
        self._fail_threshold = fail_threshold or s.breaker_fail_threshold
        self._recovery_sec = recovery_sec or s.breaker_recovery_sec
        self._half_open_max = half_open_max or s.breaker_half_open_max
        # Exceptions that propagate to the caller but do NOT count as a
        # breaker failure — they're business/validation errors, not
        # downstream-health signals (e.g. a 4xx for a bad uploaded file).
        self._ignore = ignore

        self._state = CircuitState.closed
        self._consecutive_failures = 0
        self._opened_at = 0.0
        self._half_open_calls = 0
        self._lock = asyncio.Lock()

        _REGISTRY[name] = self

    @property
    def state(self) -> CircuitState:
        return self._state

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "state": self._state.value,
            "consecutive_failures": self._consecutive_failures,
        }

    async def call(self, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        """Run `fn(*args, **kwargs)` through the breaker."""
        await self._before_call()
        try:
            result = await fn(*args, **kwargs)
        except self._ignore:
            # Business/validation error — propagate but don't penalise
            # the circuit. Still counts as "not a failure" for recovery.
            await self._on_success()
            raise
        except Exception:
            await self._on_failure()
            raise
        else:
            await self._on_success()
            return result

    # ── state transitions ──────────────────────────────────

    async def _before_call(self) -> None:
        async with self._lock:
            if self._state is CircuitState.open:
                if (time.monotonic() - self._opened_at) >= self._recovery_sec:
                    # Recovery window elapsed — probe.
                    self._state = CircuitState.half_open
                    self._half_open_calls = 0
                    logger.info("breaker.half_open", circuit=self.name)
                else:
                    raise CircuitOpenError(self.name)

            if self._state is CircuitState.half_open:
                if self._half_open_calls >= self._half_open_max:
                    # Already enough probes in flight — fail fast.
                    raise CircuitOpenError(self.name)
                self._half_open_calls += 1

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state is CircuitState.half_open:
                # A probe succeeded — close the circuit.
                logger.info("breaker.closed", circuit=self.name)
            self._state = CircuitState.closed
            self._consecutive_failures = 0
            self._half_open_calls = 0

    async def _on_failure(self) -> None:
        async with self._lock:
            self._consecutive_failures += 1
            if self._state is CircuitState.half_open:
                self._trip()
            elif self._consecutive_failures >= self._fail_threshold:
                self._trip()

    def _trip(self) -> None:
        self._state = CircuitState.open
        self._opened_at = time.monotonic()
        self._half_open_calls = 0
        logger.warning(
            "breaker.open",
            circuit=self.name,
            consecutive_failures=self._consecutive_failures,
        )


_REGISTRY: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    """Fetch (or lazily create) the named breaker."""
    if name not in _REGISTRY:
        CircuitBreaker(name)
    return _REGISTRY[name]


def all_breakers() -> list[dict[str, Any]]:
    return [b.snapshot() for b in _REGISTRY.values()]
