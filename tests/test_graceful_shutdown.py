"""
Unit tests for graceful-shutdown stream-drain logic in tbox_client.

Tested behaviours
-----------------
* Stream counter increments on entry and decrements on exit of chat_stream.
* Counter decrements even when the stream raises mid-way.
* drain_streams() returns immediately when there are no active streams.
* drain_streams() waits and returns once the last stream finishes.
* drain_streams() times out and returns (does not hang) when streams are slow.
* drain_streams() with timeout=0 skips waiting entirely.
* close_client() calls drain_streams() before closing the httpx client.
* get_active_stream_count() reflects the live count.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("TBOX_APP_ID", "test-app-id")
os.environ.setdefault("TBOX_TOKEN", "test-token")
os.environ.setdefault("API_KEYS", "test-key")

import httpx

import app.services.tbox_client as tc
from app.core.config import get_settings
from app.schemas.tbox import TBoxChatRequest
from app.utils.errors import TBoxUpstreamError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tbox_request() -> TBoxChatRequest:
    return TBoxChatRequest(appId="app", query="hello", userId="u1", stream=True)


def _reset_module_state() -> None:
    """Reset tbox_client module globals to a clean state for each test."""
    tc._active_streams = 0
    tc._streams_idle = asyncio.Event()
    tc._streams_idle.set()


# ---------------------------------------------------------------------------
# Stream counter tests (unit — no real httpx client needed)
# ---------------------------------------------------------------------------


def test_increment_sets_event_clear():
    _reset_module_state()
    assert tc._streams_idle.is_set()
    tc._increment_active_streams()
    assert not tc._streams_idle.is_set()
    assert tc.get_active_stream_count() == 1


def test_decrement_sets_event_when_zero():
    _reset_module_state()
    tc._active_streams = 1
    tc._streams_idle.clear()
    tc._decrement_active_streams()
    assert tc._streams_idle.is_set()
    assert tc.get_active_stream_count() == 0


def test_decrement_does_not_go_negative():
    _reset_module_state()
    tc._decrement_active_streams()
    assert tc.get_active_stream_count() == 0


def test_multiple_streams_count():
    _reset_module_state()
    tc._increment_active_streams()
    tc._increment_active_streams()
    assert tc.get_active_stream_count() == 2
    assert not tc._streams_idle.is_set()
    tc._decrement_active_streams()
    assert tc.get_active_stream_count() == 1
    assert not tc._streams_idle.is_set()  # still one active
    tc._decrement_active_streams()
    assert tc.get_active_stream_count() == 0
    assert tc._streams_idle.is_set()


# ---------------------------------------------------------------------------
# drain_streams tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_drain_returns_immediately_when_idle():
    _reset_module_state()
    # Should return without waiting (event is already set)
    await asyncio.wait_for(tc.drain_streams(timeout=5.0), timeout=1.0)


@pytest.mark.anyio
async def test_drain_waits_for_stream_to_finish():
    _reset_module_state()
    tc._increment_active_streams()

    async def _finish_after_delay():
        await asyncio.sleep(0.05)
        tc._decrement_active_streams()

    asyncio.create_task(_finish_after_delay())
    await asyncio.wait_for(tc.drain_streams(timeout=2.0), timeout=1.0)
    assert tc.get_active_stream_count() == 0


@pytest.mark.anyio
async def test_drain_times_out_without_hanging():
    _reset_module_state()
    tc._increment_active_streams()  # never decrement → simulates stuck stream

    # drain should return after timeout, not hang
    await asyncio.wait_for(tc.drain_streams(timeout=0.05), timeout=1.0)
    # counter still 1 after forced timeout
    assert tc.get_active_stream_count() == 1
    # cleanup
    tc._decrement_active_streams()


@pytest.mark.anyio
async def test_drain_timeout_zero_skips_wait():
    _reset_module_state()
    tc._increment_active_streams()  # never decrement

    # Should return immediately
    await asyncio.wait_for(tc.drain_streams(timeout=0), timeout=0.5)
    # cleanup
    tc._decrement_active_streams()


# ---------------------------------------------------------------------------
# chat_stream counter integration tests (mocked httpx)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_chat_stream_increments_and_decrements_counter():
    """Counter goes +1 on enter, back to 0 on normal exit."""
    _reset_module_state()

    # Provide a fake httpx client whose stream() context-manager yields bytes.
    fake_response = MagicMock()
    fake_response.status_code = 200

    async def _fake_aiter_bytes():
        yield b"data: ok\n\n"

    fake_response.aiter_bytes = _fake_aiter_bytes

    fake_stream_cm = AsyncMock()
    fake_stream_cm.__aenter__ = AsyncMock(return_value=fake_response)
    fake_stream_cm.__aexit__ = AsyncMock(return_value=False)

    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=fake_stream_cm)

    # Inject fakes
    tc._client = fake_client
    tc._circuit_breaker = MagicMock()
    tc._circuit_breaker.state = "closed"
    tc._circuit_breaker._on_success = AsyncMock()
    tc._circuit_breaker._on_failure = AsyncMock()

    req = _make_tbox_request()
    assert tc.get_active_stream_count() == 0

    async with tc.chat_stream(req) as byte_iter:
        assert tc.get_active_stream_count() == 1
        async for _ in byte_iter:
            pass

    assert tc.get_active_stream_count() == 0


@pytest.mark.anyio
async def test_chat_stream_decrements_on_error():
    """Counter still reaches 0 when the stream context manager raises."""
    _reset_module_state()

    fake_stream_cm = MagicMock()
    fake_stream_cm.__aenter__ = AsyncMock(
        side_effect=httpx.ConnectError("refused")
    )
    fake_stream_cm.__aexit__ = AsyncMock(return_value=False)

    fake_client = MagicMock()
    fake_client.stream = MagicMock(return_value=fake_stream_cm)

    tc._client = fake_client
    tc._circuit_breaker = MagicMock()
    tc._circuit_breaker.state = "closed"
    tc._circuit_breaker._on_success = AsyncMock()
    tc._circuit_breaker._on_failure = AsyncMock()

    req = _make_tbox_request()

    with pytest.raises(TBoxUpstreamError):
        async with tc.chat_stream(req) as byte_iter:
            async for _ in byte_iter:
                pass

    assert tc.get_active_stream_count() == 0


# ---------------------------------------------------------------------------
# close_client drains before closing
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_close_client_drains_before_closing():
    """close_client must await drain_streams before calling client.aclose()."""
    _reset_module_state()

    drain_called_with: list[float] = []
    aclose_called = False

    original_drain = tc.drain_streams

    async def _spy_drain(timeout: float = 30.0) -> None:
        drain_called_with.append(timeout)

    fake_client = AsyncMock()
    fake_client.aclose = AsyncMock(side_effect=lambda: _mark_aclose())

    def _mark_aclose():
        nonlocal aclose_called
        aclose_called = True

    tc._client = fake_client

    with patch.object(tc, "drain_streams", side_effect=_spy_drain):
        await tc.close_client(shutdown_timeout=15.0)

    assert drain_called_with == [15.0], "drain_streams should be called with the given timeout"
    assert aclose_called, "httpx client aclose() should be called after drain"
