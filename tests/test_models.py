"""
Tests for GET /v1/models
"""

import os
import pytest

os.environ.setdefault("TBOX_APP_ID", "test-app-id")
os.environ.setdefault("TBOX_TOKEN", "test-token")

from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.config import get_settings

AUTH_HEADERS = {"Authorization": "Bearer test-key"}


@pytest.mark.anyio
async def test_list_models_returns_200():
    """GET /v1/models should return HTTP 200."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers=AUTH_HEADERS)
    assert response.status_code == 200


@pytest.mark.anyio
async def test_list_models_shape():
    """Response must match OpenAI ModelList schema."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers=AUTH_HEADERS)
    body = response.json()
    assert body["object"] == "list"
    assert isinstance(body["data"], list)
    assert len(body["data"]) >= 1


@pytest.mark.anyio
async def test_list_models_contains_adapter_model():
    """The configured ADAPTER_MODEL_ID must appear in the model list."""
    settings = get_settings()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers=AUTH_HEADERS)
    ids = [m["id"] for m in response.json()["data"]]
    assert settings.adapter_model_id in ids


@pytest.mark.anyio
async def test_list_models_owned_by_tbox():
    """Each model card must have owned_by='tbox'."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/models", headers=AUTH_HEADERS)
    for model in response.json()["data"]:
        assert model["owned_by"] == "tbox"


@pytest.mark.anyio
async def test_health_check():
    """GET /health should return {status: ok}."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
