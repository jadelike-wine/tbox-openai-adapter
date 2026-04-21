"""
Unit tests for app/utils/resilience.py

Covers:
  - retry_async: success on first attempt, success after retries, exhaustion,
    non-retryable errors bypass retry, back-off is applied.
  - CircuitBreaker: closed→open transition, fast-fail when open,
    open→half-open→closed recovery, half-open failure re-opens.
  - resilient_call: circuit open → no retry attempted.
"""

from __future__ import annotations

import os
import pytest
import asyncio
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TBOX_APP_ID", "test-app-id")
os.environ.setdefault("TBOX_TOKEN", "test-token")
os.environ.setdefault("API_KEYS", "test-key")

import httpx

from app.utils.errors import TBoxUpstreamError
from app.utils.resilience import CircuitBreaker, retry_async, resilient_call


# ---------------------------------------------------------------------------
# Helpers — return exception *instances* so AsyncMock side_effect raises them
# ---------------------------------------------------------------------------

SERVER_ERROR = TBoxUpstreamError("upstream 502", status_code=502)
CLIENT_ERROR = TBoxUpstreamError("bad request 400", status_code=400)
TRANSPORT_ERROR = httpx.ConnectError("connection refused")


# ---------------------------------------------------------------------------
# retry_async
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_retry_success_first_attempt():
    """No retries needed when the first call succeeds."""
    fn = AsyncMock(return_value="ok")
    result = await retry_async(fn, max_attempts=3)
    assert result == "ok"
    fn.assert_awaited_once()


@pytest.mark.anyio
async def test_retry_success_after_transient_failure():
    """Should succeed on the second attempt after a transport error."""
    fn = AsyncMock(side_effect=[TRANSPORT_ERROR, "ok"])
    with patch("app.utils.resilience.asyncio.sleep", new_callable=AsyncMock):
        result = await retry_async(fn, max_attempts=3, backoff_base=0.0)
    assert result == "ok"
    assert fn.await_count == 2


@pytest.mark.anyio
async def test_retry_exhausted_raises_last_exception():
    """Should raise after all attempts are exhausted."""
    fn = AsyncMock(side_effect=[TRANSPORT_ERROR, TRANSPORT_ERROR, TRANSPORT_ERROR])
    with patch("app.utils.resilience.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(httpx.TransportError):
            await retry_async(fn, max_attempts=3, backoff_base=0.0)
    assert fn.await_count == 3


@pytest.mark.anyio
async def test_retry_server_error_is_retried():
    """5xx TBoxUpstreamError should be retried."""
    fn = AsyncMock(side_effect=[SERVER_ERROR, SERVER_ERROR, "ok"])
    with patch("app.utils.resilience.asyncio.sleep", new_callable=AsyncMock):
        result = await retry_async(fn, max_attempts=3, backoff_base=0.0)
    assert result == "ok"
    assert fn.await_count == 3


@pytest.mark.anyio
async def test_retry_client_error_not_retried():
    """4xx TBoxUpstreamError must NOT be retried — it's a client bug."""
    fn = AsyncMock(side_effect=[CLIENT_ERROR])
    with pytest.raises(TBoxUpstreamError) as exc_info:
        await retry_async(fn, max_attempts=3, backoff_base=0.0)
    assert fn.await_count == 1
    assert exc_info.value.status_code == 400


@pytest.mark.anyio
async def test_retry_max_attempts_one_means_no_retry():
    """max_attempts=1 should not retry at all."""
    fn = AsyncMock(side_effect=[TRANSPORT_ERROR])
    with pytest.raises(httpx.TransportError):
        await retry_async(fn, max_attempts=1)
    assert fn.await_count == 1


@pytest.mark.anyio
async def test_retry_sleep_is_called_between_attempts():
    """asyncio.sleep should be called between retry attempts."""
    fn = AsyncMock(side_effect=[TRANSPORT_ERROR, "ok"])
    with patch("app.utils.resilience.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await retry_async(fn, max_attempts=2, backoff_base=1.0)
    mock_sleep.assert_awaited_once()
    # Delay should be in [0, backoff_base * 2^0] = [0, 1.0]
    delay_arg = mock_sleep.await_args[0][0]
    assert 0.0 <= delay_arg <= 1.0


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cb_closed_on_init():
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0, half_open_probes=2)
    assert cb.state == "closed"


@pytest.mark.anyio
async def test_cb_opens_after_threshold():
    """Circuit should open after `failure_threshold` consecutive failures."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0, half_open_probes=2)
    fn = AsyncMock(side_effect=[SERVER_ERROR, SERVER_ERROR, SERVER_ERROR])

    for _ in range(3):
        with pytest.raises(TBoxUpstreamError):
            await cb.call(fn)

    assert cb.state == "open"


@pytest.mark.anyio
async def test_cb_open_fast_fails():
    """A call when circuit is open must raise immediately without calling fn."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=9999.0, half_open_probes=1)
    fn = AsyncMock(side_effect=[SERVER_ERROR])

    # Trip the breaker
    with pytest.raises(TBoxUpstreamError):
        await cb.call(fn)
    assert cb.state == "open"

    # Now the next call should fast-fail without invoking fn again
    fn.reset_mock()
    with pytest.raises(TBoxUpstreamError) as exc_info:
        await cb.call(fn)
    fn.assert_not_awaited()
    assert exc_info.value.status_code == 503


@pytest.mark.anyio
async def test_cb_transitions_to_half_open_after_timeout():
    """After recovery_timeout the circuit should move to half-open."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01, half_open_probes=1)
    fn = AsyncMock(side_effect=[SERVER_ERROR])

    # Trip the breaker
    with pytest.raises(TBoxUpstreamError):
        await cb.call(fn)
    assert cb.state == "open"

    # Wait for recovery
    await asyncio.sleep(0.05)

    # Successful probe should close the circuit
    fn.side_effect = None
    fn.return_value = "ok"
    result = await cb.call(fn)
    assert result == "ok"
    assert cb.state == "closed"


@pytest.mark.anyio
async def test_cb_half_open_failure_reopens():
    """A failure during half-open probing must re-open the circuit immediately."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01, half_open_probes=2)
    fn = AsyncMock(side_effect=[SERVER_ERROR])

    # Trip the breaker
    with pytest.raises(TBoxUpstreamError):
        await cb.call(fn)
    assert cb.state == "open"

    # Wait for recovery → half-open
    await asyncio.sleep(0.05)

    # Probe fails → back to open
    fn.side_effect = [SERVER_ERROR]
    with pytest.raises(TBoxUpstreamError):
        await cb.call(fn)
    assert cb.state == "open"


@pytest.mark.anyio
async def test_cb_success_resets_failure_count():
    """A success in CLOSED state should reset the consecutive failure counter."""
    cb = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0, half_open_probes=1)
    fn_fail = AsyncMock(side_effect=[SERVER_ERROR, SERVER_ERROR])
    fn_ok = AsyncMock(return_value="ok")

    # Two failures
    for _ in range(2):
        with pytest.raises(TBoxUpstreamError):
            await cb.call(fn_fail)

    # One success resets counter
    await cb.call(fn_ok)
    assert cb.state == "closed"
    assert cb._failure_count == 0

    # Two more failures should not open (threshold is 3)
    fn_fail2 = AsyncMock(side_effect=[SERVER_ERROR, SERVER_ERROR])
    for _ in range(2):
        with pytest.raises(TBoxUpstreamError):
            await cb.call(fn_fail2)
    assert cb.state == "closed"


# ---------------------------------------------------------------------------
# resilient_call
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_resilient_call_open_circuit_no_retry():
    """When circuit is open, resilient_call must fail fast — no retry."""
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout=9999.0, half_open_probes=1)
    fn = AsyncMock(side_effect=[SERVER_ERROR])

    # Trip the circuit
    with pytest.raises(TBoxUpstreamError):
        await cb.call(fn)
    fn.reset_mock()

    # resilient_call should not invoke fn at all
    with pytest.raises(TBoxUpstreamError) as exc_info:
        await resilient_call(fn, cb, max_attempts=3, operation="test")
    fn.assert_not_awaited()
    assert exc_info.value.status_code == 503


@pytest.mark.anyio
async def test_resilient_call_retries_and_succeeds():
    """resilient_call should retry a transient error and eventually succeed."""
    cb = CircuitBreaker(failure_threshold=10, recovery_timeout=30.0, half_open_probes=1)
    fn = AsyncMock(side_effect=[TRANSPORT_ERROR, "ok"])
    with patch("app.utils.resilience.asyncio.sleep", new_callable=AsyncMock):
        result = await resilient_call(fn, cb, max_attempts=3, backoff_base=0.0, operation="test")
    assert result == "ok"
    assert fn.await_count == 2
    assert cb.state == "closed"
