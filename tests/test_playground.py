import pytest

from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.anyio
async def test_playground_page_is_public():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/playground")

    assert response.status_code == 200
    assert "TBox Adapter Web UI" in response.text


@pytest.mark.anyio
async def test_root_serves_playground_page():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/")

    assert response.status_code == 200
    assert "TBox Adapter Web UI" in response.text


@pytest.mark.anyio
async def test_playground_static_asset_is_public():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/playground-static/playground.js")

    assert response.status_code == 200
    assert "streamRequest" in response.text
