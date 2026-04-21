"""
Tests for POST /v1/chat/completions

Real TBox HTTP calls are patched with unittest.mock so tests run offline.
"""

import os
import pytest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("TBOX_APP_ID", "test-app-id")
os.environ.setdefault("TBOX_TOKEN", "test-token")

from httpx import AsyncClient, ASGITransport

from app.main import app
from app.stores import session_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAT_URL = "/v1/chat/completions"
AUTH_HEADERS = {"Authorization": "Bearer test-key"}

NON_STREAM_PAYLOAD = {
    "model": "tbox-codex",
    "messages": [{"role": "user", "content": "Hello, TBox!"}],
    "stream": False,
    "user": "test-user",
}

STREAM_PAYLOAD = {**NON_STREAM_PAYLOAD, "stream": True}


# Fake TBox non-streaming response
FAKE_TBOX_RESPONSE = {
    "conversationId": "conv-abc123",
    "text": "Hello from TBox!",
}

# Fake TBox SSE events as raw bytes
FAKE_SSE_BYTES = (
    b'data: {"event": "header", "payload": {"conversationId": "conv-sse-001"}}\n\n'
    b'data: {"event": "chunk", "payload": {"text": "Hello "}}\n\n'
    b'data: {"event": "chunk", "payload": {"text": "world!"}}\n\n'
    b'data: {"event": "meta", "payload": {"tokens": 10}}\n\n'
    b'data: [DONE]\n\n'
)


# ---------------------------------------------------------------------------
# Non-streaming tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_chat_non_stream_returns_200():
    """Non-streaming chat request should return HTTP 200."""
    with patch(
        "app.services.tbox_client.chat_once",
        new_callable=AsyncMock,
        return_value=FAKE_TBOX_RESPONSE,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(CHAT_URL, json=NON_STREAM_PAYLOAD, headers=AUTH_HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
async def test_chat_non_stream_response_shape():
    """Non-streaming response must match OpenAI ChatCompletion schema."""
    with patch(
        "app.services.tbox_client.chat_once",
        new_callable=AsyncMock,
        return_value=FAKE_TBOX_RESPONSE,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(CHAT_URL, json=NON_STREAM_PAYLOAD, headers=AUTH_HEADERS)
    body = response.json()
    assert body["object"] == "chat.completion"
    assert len(body["choices"]) == 1
    choice = body["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "Hello from TBox!"
    assert choice["finish_reason"] == "stop"


@pytest.mark.anyio
async def test_chat_non_stream_updates_session():
    """Non-streaming response should persist conversationId in session store."""
    session_store.clear_conversation("test-user-session")
    with patch(
        "app.services.tbox_client.chat_once",
        new_callable=AsyncMock,
        return_value=FAKE_TBOX_RESPONSE,
    ):
        payload = {**NON_STREAM_PAYLOAD, "user": "test-user-session"}
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            await client.post(CHAT_URL, json=payload, headers=AUTH_HEADERS)
    assert session_store.get_conversation_id("test-user-session") == "conv-abc123"


@pytest.mark.anyio
async def test_chat_forwards_system_and_developer_prompt():
    payload = {
        "model": "tbox-codex",
        "messages": [
            {"role": "system", "content": "system rule"},
            {"role": "developer", "content": "developer rule"},
            {"role": "user", "content": "hello"},
        ],
        "stream": False,
    }
    with patch(
        "app.services.tbox_client.chat_once",
        new_callable=AsyncMock,
        return_value=FAKE_TBOX_RESPONSE,
    ) as mock_chat_once:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(CHAT_URL, json=payload, headers=AUTH_HEADERS)

    assert response.status_code == 200
    req = mock_chat_once.await_args.args[0]
    assert req.systemPrompt == "system rule\ndeveloper rule"


@pytest.mark.anyio
async def test_chat_no_user_message_returns_400():
    """Request with no user-role message should return 400."""
    payload = {
        "model": "tbox-codex",
        "messages": [{"role": "system", "content": "You are helpful."}],
        "stream": False,
    }
    with patch(
        "app.services.tbox_client.chat_once",
        new_callable=AsyncMock,
        return_value=FAKE_TBOX_RESPONSE,
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(CHAT_URL, json=payload, headers=AUTH_HEADERS)
    assert response.status_code == 400
    assert "error" in response.json()


# ---------------------------------------------------------------------------
# Streaming tests
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_chat_stream_returns_200():
    """Streaming request should return HTTP 200 with text/event-stream content type."""

    async def fake_byte_iter():
        yield FAKE_SSE_BYTES

    with patch(
        "app.services.tbox_client._get_client"
    ) as mock_get_client:
        # We patch at a lower level for the streaming context manager
        # Instead, patch handle_chat_stream directly
        pass

    # Patch the generator function directly
    async def fake_stream_gen(request, settings):
        yield 'data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"model":"tbox-codex","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}\n\n'
        yield 'data: {"id":"chatcmpl-x","object":"chat.completion.chunk","created":1,"model":"tbox-codex","choices":[{"index":0,"delta":{"content":"Hello world!"},"finish_reason":null}]}\n\n'
        yield "data: [DONE]\n\n"

    with patch("app.services.chat_adapter.handle_chat_stream", side_effect=fake_stream_gen):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(CHAT_URL, json=STREAM_PAYLOAD, headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")


@pytest.mark.anyio
async def test_chat_stream_contains_done_sentinel():
    """SSE stream must end with the [DONE] sentinel."""

    async def fake_stream_gen(request, settings):
        yield 'data: {"id":"x","object":"chat.completion.chunk","created":1,"model":"tbox-codex","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n\n'
        yield "data: [DONE]\n\n"

    with patch("app.services.chat_adapter.handle_chat_stream", side_effect=fake_stream_gen):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(CHAT_URL, json=STREAM_PAYLOAD, headers=AUTH_HEADERS)

    assert "data: [DONE]" in response.text


# ---------------------------------------------------------------------------
# Session store unit tests
# ---------------------------------------------------------------------------


def test_session_store_set_and_get():
    session_store.clear_conversation("unit-test-user")
    assert session_store.get_conversation_id("unit-test-user") is None
    session_store.set_conversation_id("unit-test-user", "conv-999")
    assert session_store.get_conversation_id("unit-test-user") == "conv-999"


def test_session_store_clear():
    session_store.set_conversation_id("unit-test-clear", "conv-clear")
    session_store.clear_conversation("unit-test-clear")
    assert session_store.get_conversation_id("unit-test-clear") is None
