"""
Low-level async HTTP client for the TBox API.

Responsibilities:
  - Build authenticated requests (Authorization header, appId injection)
  - Handle non-2xx responses by raising TBoxUpstreamError
  - Provide both a one-shot JSON method and a streaming byte-iterator method
  - Keep a single shared httpx.AsyncClient per process (created at startup)
  - Wrap every non-streaming call with retry + circuit-breaker via resilience.py

Retry / circuit-breaker policy
-------------------------------
  Non-streaming calls (chat_once, upload_file, retrieve_file,
  create_conversation, list_conversations, list_messages):
    → retried up to TBOX_RETRY_MAX_ATTEMPTS times with exponential back-off,
      wrapped in a shared circuit breaker.

  Streaming calls (chat_stream):
    → NOT retried (mid-stream retry semantics are ambiguous), but still guarded
      by the circuit breaker so an open circuit fails fast before connecting.

This module is intentionally thin — it only speaks to TBox.
All OpenAI <-> TBox translation lives in chat_adapter.py.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import httpx

from app.core.config import Settings
from app.schemas.tbox import TBoxChatRequest
from app.utils.errors import TBoxUpstreamError
from app.utils.metrics import (
    ACTIVE_SSE_STREAMS,
    CIRCUIT_BREAKER_STATE,
    TBOX_UPSTREAM_DURATION_SECONDS,
    TBOX_UPSTREAM_ERRORS_TOTAL,
    TBOX_UPSTREAM_REQUESTS_TOTAL,
)
from app.utils.resilience import CircuitBreaker, resilient_call

logger = logging.getLogger(__name__)

# Module-level client instance, initialised by create_client() at app startup.
_client: Optional[httpx.AsyncClient] = None

# Single circuit breaker shared across all TBox calls in this process.
_circuit_breaker: Optional[CircuitBreaker] = None

# Retry configuration — populated by create_client().
_retry_max_attempts: int = 3
_retry_backoff_base: float = 0.5
_retry_backoff_max: float = 10.0

# ---------------------------------------------------------------------------
# Active-stream tracking — used for graceful shutdown
# ---------------------------------------------------------------------------
# Count of SSE streams currently open against TBox.
_active_streams: int = 0
# Event that is set whenever _active_streams reaches zero.
# Shutdown waits on this event so it doesn't close the httpx client while
# data is still flowing.
_streams_idle: asyncio.Event = asyncio.Event()
_streams_idle.set()  # starts idle (no active streams)


def create_client(settings: Settings) -> httpx.AsyncClient:
    """
    Instantiate and store the shared httpx.AsyncClient and CircuitBreaker.

    Call this once during application lifespan startup.
    """
    global _client, _circuit_breaker
    global _retry_max_attempts, _retry_backoff_base, _retry_backoff_max
    global _active_streams, _streams_idle

    # Reset stream-tracking state so tests and restarts start clean.
    _active_streams = 0
    _streams_idle = asyncio.Event()
    _streams_idle.set()

    _client = httpx.AsyncClient(
        base_url=settings.tbox_base_url,
        headers={
            "Authorization": f"Bearer {settings.tbox_token}",
            "Content-Type": "application/json",
        },
        timeout=httpx.Timeout(settings.tbox_timeout),
    )

    _circuit_breaker = CircuitBreaker(
        failure_threshold=settings.tbox_cb_failure_threshold,
        recovery_timeout=settings.tbox_cb_recovery_timeout,
        half_open_probes=settings.tbox_cb_half_open_probes,
        name="tbox",
    )

    _retry_max_attempts = settings.tbox_retry_max_attempts
    _retry_backoff_base = settings.tbox_retry_backoff_base
    _retry_backoff_max = settings.tbox_retry_backoff_max

    logger.info(
        "TBox HTTP client created — base_url=%s retry_max=%d cb_threshold=%d cb_recovery=%.0fs",
        settings.tbox_base_url,
        _retry_max_attempts,
        settings.tbox_cb_failure_threshold,
        settings.tbox_cb_recovery_timeout,
    )
    return _client


async def drain_streams(timeout: float = 30.0) -> None:
    """
    Wait until all active SSE streams have finished, or until *timeout* seconds
    have elapsed — whichever comes first.

    Called by close_client() as part of graceful shutdown so that in-flight
    SSE connections are not severed while data is still flowing to clients.

    Parameters
    ----------
    timeout:
        Maximum seconds to wait.  Use 0 to skip draining entirely.
    """
    if _active_streams == 0:
        return  # nothing to wait for

    if timeout <= 0:
        logger.warning(
            "Graceful shutdown: drain disabled (timeout=0); "
            "closing %d active stream(s) immediately",
            _active_streams,
        )
        return

    logger.info(
        "Graceful shutdown: waiting up to %.0fs for %d active stream(s) to finish …",
        timeout,
        _active_streams,
    )
    try:
        await asyncio.wait_for(_streams_idle.wait(), timeout=timeout)
        logger.info("Graceful shutdown: all streams finished cleanly")
    except asyncio.TimeoutError:
        logger.warning(
            "Graceful shutdown: timeout after %.0fs — "
            "%d stream(s) still active, closing forcefully",
            timeout,
            _active_streams,
        )


async def close_client(shutdown_timeout: float = 30.0) -> None:
    """
    Drain active SSE streams, then close the shared httpx client.

    Call during application lifespan shutdown.

    Parameters
    ----------
    shutdown_timeout:
        Forwarded to drain_streams().
    """
    global _client, _circuit_breaker
    await drain_streams(shutdown_timeout)
    if _client is not None:
        await _client.aclose()
        _client = None
        logger.info("TBox HTTP client closed")
    _circuit_breaker = None


def _get_client() -> httpx.AsyncClient:
    if _client is None:
        raise RuntimeError(
            "TBox HTTP client is not initialised. "
            "Ensure create_client() is called during app startup."
        )
    return _client


def _get_circuit_breaker() -> CircuitBreaker:
    if _circuit_breaker is None:
        raise RuntimeError(
            "TBox circuit breaker is not initialised. "
            "Ensure create_client() is called during app startup."
        )
    return _circuit_breaker


def _increment_active_streams() -> None:
    """Record that one more SSE stream is now open."""
    global _active_streams, _streams_idle
    _active_streams += 1
    _streams_idle.clear()  # no longer idle
    ACTIVE_SSE_STREAMS.set(_active_streams)


def _decrement_active_streams() -> None:
    """Record that one SSE stream has closed."""
    global _active_streams, _streams_idle
    _active_streams = max(0, _active_streams - 1)
    if _active_streams == 0:
        _streams_idle.set()  # signal that we are idle again
    ACTIVE_SSE_STREAMS.set(_active_streams)


def get_active_stream_count() -> int:
    """Return the number of SSE streams currently open (for monitoring / tests)."""
    return _active_streams


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _raise_for_status(response: httpx.Response, context: str) -> None:
    """Raise TBoxUpstreamError for non-2xx responses."""
    if response.status_code >= 400:
        raise TBoxUpstreamError(
            f"{context} returned HTTP {response.status_code}: {response.text}",
            status_code=502,
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def chat_once(request: TBoxChatRequest) -> dict:
    """
    Send a non-streaming chat request to TBox and return the parsed JSON body.

    Retried on network errors and 5xx responses.
    Fails fast when the circuit breaker is OPEN.

    Raises TBoxUpstreamError on non-2xx responses or when the circuit is open.
    """
    import time as _time

    client = _get_client()
    cb = _get_circuit_breaker()
    payload = request.model_dump(exclude_none=True)
    logger.debug("TBox chat_once payload: %s", payload)

    async def _call() -> dict:
        try:
            response = await client.post("/api/chat", json=payload)
        except httpx.RequestError as exc:
            raise TBoxUpstreamError(f"TBox request failed: {exc}") from exc
        _raise_for_status(response, "TBox chat_once")
        return response.json()

    _t0 = _time.perf_counter()
    try:
        result = await resilient_call(
            _call,
            cb,
            max_attempts=_retry_max_attempts,
            backoff_base=_retry_backoff_base,
            backoff_max=_retry_backoff_max,
            operation="chat_once",
        )
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="chat_once", status="success").inc()
        return result
    except TBoxUpstreamError as exc:
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="chat_once", status="error").inc()
        TBOX_UPSTREAM_ERRORS_TOTAL.labels(
            operation="chat_once",
            error_type=type(exc).__name__,
        ).inc()
        raise
    except Exception as exc:
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="chat_once", status="error").inc()
        TBOX_UPSTREAM_ERRORS_TOTAL.labels(
            operation="chat_once",
            error_type=type(exc).__name__,
        ).inc()
        raise
    finally:
        TBOX_UPSTREAM_DURATION_SECONDS.labels(operation="chat_once").observe(
            _time.perf_counter() - _t0
        )


@asynccontextmanager
async def chat_stream(request: TBoxChatRequest) -> AsyncIterator[AsyncIterator[bytes]]:
    """
    Send a streaming chat request to TBox.

    The circuit breaker is checked **before** opening the connection; if it is
    OPEN the function raises TBoxUpstreamError immediately without contacting
    TBox.  Retry is NOT applied to the stream (mid-stream retries would deliver
    duplicate tokens to the client).

    Usage:
        async with chat_stream(req) as byte_iter:
            async for chunk in byte_iter:
                ...

    Yields an async iterator of raw bytes from the SSE stream.
    Raises TBoxUpstreamError if TBox returns a non-2xx status line or the
    circuit is open.
    """
    client = _get_client()
    cb = _get_circuit_breaker()
    payload = request.model_dump(exclude_none=True)
    payload["stream"] = True
    logger.debug("TBox chat_stream payload: %s", payload)

    # Fast-fail if circuit is open — check before entering the async-with block.
    # We call circuit_breaker.call() with a no-op to trigger the state check.
    async def _pre_check() -> None:
        pass  # just let the circuit breaker gate the call

    # We need to check the CB state without running the full request inside it
    # (the context manager can't be used inside circuit_breaker.call).
    # Instead, do a lightweight state probe.
    if cb.state == "open":
        # Trigger the recovery-timeout check (may transition to half-open)
        try:
            await cb.call(_pre_check)
        except TBoxUpstreamError:
            raise

    # --- Graceful-shutdown tracking ---
    # Increment before opening the connection; decrement in the finally block
    # regardless of whether the stream completes normally or raises.
    import time as _time

    _t0 = _time.perf_counter()
    _increment_active_streams()
    try:
        try:
            async with client.stream("POST", "/api/chat", json=payload) as response:
                if response.status_code >= 400:
                    body = await response.aread()
                    exc = TBoxUpstreamError(
                        f"TBox stream returned HTTP {response.status_code}: "
                        f"{body.decode(errors='replace')}",
                        status_code=502,
                    )
                    await cb._on_failure(exc)
                    TBOX_UPSTREAM_REQUESTS_TOTAL.labels(
                        operation="chat_stream", status="error"
                    ).inc()
                    TBOX_UPSTREAM_ERRORS_TOTAL.labels(
                        operation="chat_stream",
                        error_type="TBoxUpstreamError",
                    ).inc()
                    raise exc
                # Stream opened successfully — record as a success for the CB.
                await cb._on_success()
                TBOX_UPSTREAM_REQUESTS_TOTAL.labels(
                    operation="chat_stream", status="success"
                ).inc()
                yield response.aiter_bytes()
        except httpx.RequestError as exc:
            tb_exc = TBoxUpstreamError(f"TBox stream request failed: {exc}")
            tb_exc.__cause__ = exc
            await cb._on_failure(tb_exc)
            TBOX_UPSTREAM_REQUESTS_TOTAL.labels(
                operation="chat_stream", status="error"
            ).inc()
            TBOX_UPSTREAM_ERRORS_TOTAL.labels(
                operation="chat_stream",
                error_type="RequestError",
            ).inc()
            raise tb_exc
    finally:
        TBOX_UPSTREAM_DURATION_SECONDS.labels(operation="chat_stream").observe(
            _time.perf_counter() - _t0
        )
        _decrement_active_streams()


async def upload_file(file_bytes: bytes, filename: str, content_type: str) -> dict:
    """
    Upload a file to TBox and return the full response dict.

    Retried on network errors and 5xx responses.
    """
    import time as _time

    client = _get_client()
    cb = _get_circuit_breaker()
    upload_headers = {"Authorization": client.headers["Authorization"]}
    files = {"file": (filename, file_bytes, content_type)}

    async def _call() -> dict:
        try:
            response = await client.post(
                "/api/file/upload",
                files=files,
                headers=upload_headers,
            )
        except httpx.RequestError as exc:
            raise TBoxUpstreamError(f"TBox file upload failed: {exc}") from exc
        _raise_for_status(response, "TBox file upload")
        return response.json()

    _t0 = _time.perf_counter()
    try:
        result = await resilient_call(
            _call,
            cb,
            max_attempts=_retry_max_attempts,
            backoff_base=_retry_backoff_base,
            backoff_max=_retry_backoff_max,
            operation="upload_file",
        )
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="upload_file", status="success").inc()
        return result
    except Exception as exc:
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="upload_file", status="error").inc()
        TBOX_UPSTREAM_ERRORS_TOTAL.labels(
            operation="upload_file", error_type=type(exc).__name__
        ).inc()
        raise
    finally:
        TBOX_UPSTREAM_DURATION_SECONDS.labels(operation="upload_file").observe(
            _time.perf_counter() - _t0
        )


async def retrieve_file(file_id: str) -> dict:
    """
    Retrieve file details from TBox by fileId.

    Retried on network errors and 5xx responses.
    """
    import time as _time

    client = _get_client()
    cb = _get_circuit_breaker()

    async def _call() -> dict:
        try:
            response = await client.get("/api/file/retrieve", params={"fileId": file_id})
        except httpx.RequestError as exc:
            raise TBoxUpstreamError(f"TBox file retrieve failed: {exc}") from exc
        _raise_for_status(response, "TBox file retrieve")
        return response.json()

    _t0 = _time.perf_counter()
    try:
        result = await resilient_call(
            _call,
            cb,
            max_attempts=_retry_max_attempts,
            backoff_base=_retry_backoff_base,
            backoff_max=_retry_backoff_max,
            operation="retrieve_file",
        )
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="retrieve_file", status="success").inc()
        return result
    except Exception as exc:
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="retrieve_file", status="error").inc()
        TBOX_UPSTREAM_ERRORS_TOTAL.labels(
            operation="retrieve_file", error_type=type(exc).__name__
        ).inc()
        raise
    finally:
        TBOX_UPSTREAM_DURATION_SECONDS.labels(operation="retrieve_file").observe(
            _time.perf_counter() - _t0
        )


async def create_conversation(app_id: str) -> dict:
    """
    Create a new TBox conversation for the given appId.

    Retried on network errors and 5xx responses.
    """
    import time as _time

    client = _get_client()
    cb = _get_circuit_breaker()

    async def _call() -> dict:
        try:
            response = await client.post(
                "/api/conversation/create",
                json={"appId": app_id},
            )
        except httpx.RequestError as exc:
            raise TBoxUpstreamError(f"TBox create conversation failed: {exc}") from exc
        _raise_for_status(response, "TBox create conversation")
        return response.json()

    _t0 = _time.perf_counter()
    try:
        result = await resilient_call(
            _call,
            cb,
            max_attempts=_retry_max_attempts,
            backoff_base=_retry_backoff_base,
            backoff_max=_retry_backoff_max,
            operation="create_conversation",
        )
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="create_conversation", status="success").inc()
        return result
    except Exception as exc:
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="create_conversation", status="error").inc()
        TBOX_UPSTREAM_ERRORS_TOTAL.labels(
            operation="create_conversation", error_type=type(exc).__name__
        ).inc()
        raise
    finally:
        TBOX_UPSTREAM_DURATION_SECONDS.labels(operation="create_conversation").observe(
            _time.perf_counter() - _t0
        )


async def list_conversations(
    app_id: str,
    user_id: Optional[str] = None,
    source: Optional[str] = None,
    page_num: int = 1,
    page_size: int = 10,
    sort_order: str = "DESC",
) -> dict:
    """
    Query the conversation list for the given appId.

    Retried on network errors and 5xx responses.
    """
    client = _get_client()
    cb = _get_circuit_breaker()
    params: dict = {
        "appId": app_id,
        "pageNum": page_num,
        "pageSize": page_size,
        "sortOrder": sort_order,
    }
    if user_id:
        params["userId"] = user_id
    if source:
        params["source"] = source

    async def _call() -> dict:
        try:
            response = await client.get("/api/conversation/conversations", params=params)
        except httpx.RequestError as exc:
            raise TBoxUpstreamError(f"TBox list conversations failed: {exc}") from exc
        _raise_for_status(response, "TBox list conversations")
        return response.json()

    import time as _time

    _t0 = _time.perf_counter()
    try:
        result = await resilient_call(
            _call,
            cb,
            max_attempts=_retry_max_attempts,
            backoff_base=_retry_backoff_base,
            backoff_max=_retry_backoff_max,
            operation="list_conversations",
        )
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="list_conversations", status="success").inc()
        return result
    except Exception as exc:
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="list_conversations", status="error").inc()
        TBOX_UPSTREAM_ERRORS_TOTAL.labels(
            operation="list_conversations", error_type=type(exc).__name__
        ).inc()
        raise
    finally:
        TBOX_UPSTREAM_DURATION_SECONDS.labels(operation="list_conversations").observe(
            _time.perf_counter() - _t0
        )


async def list_messages(
    conversation_id: str,
    page_num: int = 1,
    page_size: int = 10,
    sort_order: str = "DESC",
) -> dict:
    """
    Query the message list for a given conversationId.

    Retried on network errors and 5xx responses.
    """
    client = _get_client()
    cb = _get_circuit_breaker()
    params: dict = {
        "conversationId": conversation_id,
        "pageNum": page_num,
        "pageSize": page_size,
        "sortOrder": sort_order,
    }

    async def _call() -> dict:
        try:
            response = await client.get("/api/conversation/messages", params=params)
        except httpx.RequestError as exc:
            raise TBoxUpstreamError(f"TBox list messages failed: {exc}") from exc
        _raise_for_status(response, "TBox list messages")
        return response.json()

    import time as _time

    _t0 = _time.perf_counter()
    try:
        result = await resilient_call(
            _call,
            cb,
            max_attempts=_retry_max_attempts,
            backoff_base=_retry_backoff_base,
            backoff_max=_retry_backoff_max,
            operation="list_messages",
        )
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="list_messages", status="success").inc()
        return result
    except Exception as exc:
        TBOX_UPSTREAM_REQUESTS_TOTAL.labels(operation="list_messages", status="error").inc()
        TBOX_UPSTREAM_ERRORS_TOTAL.labels(
            operation="list_messages", error_type=type(exc).__name__
        ).inc()
        raise
    finally:
        TBOX_UPSTREAM_DURATION_SECONDS.labels(operation="list_messages").observe(
            _time.perf_counter() - _t0
        )
