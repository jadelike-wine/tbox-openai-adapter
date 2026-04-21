import pytest
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from app.main import app

AUTH_HEADERS = {"Authorization": "Bearer test-key"}
MESSAGES_URL = "/anthropic/v1/messages"


@pytest.mark.anyio
async def test_anthropic_non_stream_returns_200():
    payload = {
        "model": "claude-test",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }
    with patch(
        "app.services.tbox_client.chat_once",
        new_callable=AsyncMock,
        return_value={"conversationId": "conv-anth-1", "text": "hello back"},
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(MESSAGES_URL, json=payload, headers=AUTH_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "hello back"


@pytest.mark.anyio
async def test_anthropic_stream_returns_done_sentinel():
    payload = {
        "model": "claude-test",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
    }

    async def fake_stream_gen(request, settings):
        yield "event: message_start\\ndata: {\"type\":\"message_start\"}\\n\\n"
        yield "data: [DONE]\\n\\n"

    with patch("app.services.anthropic_adapter.handle_messages_stream", side_effect=fake_stream_gen):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(MESSAGES_URL, json=payload, headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert "data: [DONE]" in response.text


@pytest.mark.anyio
async def test_anthropic_file_block_is_forwarded_to_tbox():
    payload = {
        "model": "claude-test",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "read this"},
                    {
                        "type": "file",
                        "file_kind": "FILE",
                        "source": {"type": "file", "file_id": "file_123"},
                    },
                ],
            }
        ],
        "stream": False,
    }

    with patch(
        "app.services.tbox_client.chat_once",
        new_callable=AsyncMock,
        return_value={"conversationId": "conv-anth-2", "text": "ok"},
    ) as mock_chat_once:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(MESSAGES_URL, json=payload, headers=AUTH_HEADERS)

    assert response.status_code == 200
    req = mock_chat_once.await_args.args[0]
    assert req.files is not None
    assert len(req.files) == 1
    assert req.files[0].fileId == "file_123"
