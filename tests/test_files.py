import pytest
from unittest.mock import AsyncMock, patch

from httpx import ASGITransport, AsyncClient

from app.main import app

AUTH_HEADERS = {"Authorization": "Bearer test-key"}


@pytest.mark.anyio
async def test_file_upload_route():
    with patch(
        "app.services.tbox_client.upload_file",
        new_callable=AsyncMock,
        return_value={"errorCode": "SUCCESS", "errorMsg": "", "data": "file_1"},
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(
                "/openai/v1/files",
                files={"file": ("hello.txt", b"hello", "text/plain")},
                headers=AUTH_HEADERS,
            )

    assert response.status_code == 200
    assert response.json()["data"] == "file_1"


@pytest.mark.anyio
async def test_file_retrieve_route():
    with patch(
        "app.services.tbox_client.retrieve_file",
        new_callable=AsyncMock,
        return_value={
            "errorCode": "SUCCESS",
            "errorMsg": "",
            "data": {
                "id": "file_1",
                "fileName": "hello.txt",
                "fileType": "txt",
                "bytes": 5,
            },
        },
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get("/openai/v1/files/file_1", headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["data"]["id"] == "file_1"


@pytest.mark.anyio
async def test_validation_error_uses_openai_error_shape():
    # Missing required `model` and `messages` fields
    bad_payload = {"stream": False}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/chat/completions", json=bad_payload, headers=AUTH_HEADERS)

    assert response.status_code == 400
    body = response.json()
    assert "error" in body
    assert body["error"]["type"] == "invalid_request_error"
    assert body["error"]["code"] == "invalid_request_error"
