"""
Resilience utilities: retry with exponential back-off + circuit breaker.

Design goals
------------
* Zero extra dependencies — built on asyncio and the stdlib only.
* Retry is applied to **non-streaming** HTTP calls only.  Streaming calls
  skip retry (semantics are ambiguous once the SSE stream has started) but
  still go through the circuit breaker.
* Circuit breaker uses a simple consecutive-failure counter (not a sliding
  time-window) which is correct for a single-process adapter.

Circuit breaker states
----------------------
  CLOSED     — normal operation; failures are counted.
  OPEN       — fast-fail; no requests forwarded to TBox.
               After `recovery_timeout` seconds it transitions to HALF_OPEN.
  HALF_OPEN  — probe mode; up to `half_open_probes` consecutive successes
               close the circuit; any failure re-opens it immediately.

Retry policy
------------
  * Retried errors: httpx.TransportError (ConnectError, ReadTimeout, etc.)
    and TBoxUpstreamError with status_code >= 500.
  * Not retried: TBoxUpstreamError with status_code 4xx (client error).
  * Back-off: `min(backoff_base * 2^attempt, backoff_max)` with full jitter.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from app.utils.errors import TBoxUpstreamError

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class _State(enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """
    Async-safe circuit breaker for a single upstream dependency.

    Thread-safety note: asyncio is single-threaded per event-loop, so a plain
    asyncio.Lock is sufficient — no need for threading.Lock.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_probes: int = 2,
        name: str = "tbox",
    ) -> None:
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._half_open_probes = half_open_probes
        self._name = name

        self._state = _State.CLOSED
        self._failure_count: int = 0
        self._success_count: int = 0          # used in HALF_OPEN
        self._opened_at: float = 0.0

        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state.value

    async def call(self, coro_fn: Callable[[], Awaitable[T]]) -> T:
        """
        Execute *coro_fn* under circuit-breaker protection.

        Raises TBoxUpstreamError immediately when the circuit is OPEN.
        """
        async with self._lock:
            await self._maybe_transition()
            if self._state is _State.OPEN:
                raise TBoxUpstreamError(
                    f"Circuit breaker [{self._name}] is OPEN — "
                    f"TBox appears to be unavailable. "
                    f"Retry after {self._recovery_timeout:.0f}s.",
                    status_code=503,
                )

        try:
            result = await coro_fn()
        except Exception as exc:
            await self._on_failure(exc)
            raise
        else:
            await self._on_success()
            return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _maybe_transition(self) -> None:
        """Check whether an OPEN circuit should move to HALF_OPEN."""
        if self._state is _State.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._recovery_timeout:
                self._state = _State.HALF_OPEN
                self._success_count = 0
                logger.info(
                    "Circuit breaker [%s] → HALF_OPEN after %.1fs",
                    self._name,
                    elapsed,
                )

    async def _on_success(self) -> None:
        async with self._lock:
            if self._state is _State.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._half_open_probes:
                    self._state = _State.CLOSED
                    self._failure_count = 0
                    logger.info(
                        "Circuit breaker [%s] → CLOSED after %d probe(s)",
                        self._name,
                        self._success_count,
                    )
            elif self._state is _State.CLOSED:
                # Reset consecutive failure counter on any success
                self._failure_count = 0

    async def _on_failure(self, exc: Exception) -> None:
        async with self._lock:
            if self._state is _State.HALF_OPEN:
                # Any failure in probe mode re-opens immediately
                self._state = _State.OPEN
                self._opened_at = time.monotonic()
                logger.warning(
                    "Circuit breaker [%s] → OPEN (probe failed: %s)",
                    self._name,
                    exc,
                )
            elif self._state is _State.CLOSED:
                self._failure_count += 1
                logger.debug(
                    "Circuit breaker [%s] failure %d/%d: %s",
                    self._name,
                    self._failure_count,
                    self._failure_threshold,
                    exc,
                )
                if self._failure_count >= self._failure_threshold:
                    self._state = _State.OPEN
                    self._opened_at = time.monotonic()
                    logger.error(
                        "Circuit breaker [%s] → OPEN after %d consecutive failures",
                        self._name,
                        self._failure_count,
                    )


# ---------------------------------------------------------------------------
# Retry with exponential back-off + full jitter
# ---------------------------------------------------------------------------


def _is_retryable(exc: Exception) -> bool:
    """Return True if *exc* warrants a retry attempt."""
    if isinstance(exc, httpx.TransportError):
        # ConnectError, ReadTimeout, WriteTimeout, PoolTimeout, etc.
        return True
    if isinstance(exc, TBoxUpstreamError):
        # Retry server-side errors; do NOT retry client errors (4xx)
        return exc.status_code >= 500
    return False


async def retry_async(
    coro_fn: Callable[[], Awaitable[T]],
    *,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    backoff_max: float = 10.0,
    operation: str = "tbox_request",
) -> T:
    """
    Execute *coro_fn* with exponential back-off retry.

    Parameters
    ----------
    coro_fn:
        A zero-argument async callable that performs the operation.
    max_attempts:
        Total number of attempts (1 means no retry).
    backoff_base:
        Initial delay in seconds; delay = min(base * 2^n, max) * random(0, 1).
    backoff_max:
        Upper bound on computed delay.
    operation:
        Label used in log messages.

    Raises the last exception if all attempts are exhausted.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await coro_fn()
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                raise
            if attempt == max_attempts:
                logger.error(
                    "retry [%s] all %d attempt(s) exhausted: %s",
                    operation,
                    max_attempts,
                    exc,
                )
                raise

            # Full-jitter exponential back-off
            delay = min(backoff_base * (2 ** (attempt - 1)), backoff_max)
            jittered = random.uniform(0, delay)
            logger.warning(
                "retry [%s] attempt %d/%d failed (%s) — retrying in %.2fs",
                operation,
                attempt,
                max_attempts,
                exc,
                jittered,
            )
            await asyncio.sleep(jittered)

    # Unreachable, but satisfies type checkers
    raise last_exc  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Convenience combinator: retry inside circuit breaker
# ---------------------------------------------------------------------------


async def resilient_call(
    coro_fn: Callable[[], Awaitable[T]],
    circuit_breaker: CircuitBreaker,
    *,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    backoff_max: float = 10.0,
    operation: str = "tbox_request",
) -> T:
    """
    Execute *coro_fn* with both retry and circuit-breaker protection.

    The circuit breaker wraps the retry loop so that:
    - A fast-fail happens immediately when the circuit is OPEN (no retries).
    - Each individual attempt increments the failure counter independently.
    """
    async def _with_retry() -> T:
        return await retry_async(
            coro_fn,
            max_attempts=max_attempts,
            backoff_base=backoff_base,
            backoff_max=backoff_max,
            operation=operation,
        )

    return await circuit_breaker.call(_with_retry)
