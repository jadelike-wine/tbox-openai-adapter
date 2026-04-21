"""
Shared pytest fixtures.

Sets up a fake .env so tests don't require real TBox credentials.
The TBOX_APP_ID and TBOX_TOKEN are mocked; actual HTTP calls are
intercepted in individual test files using httpx.MockTransport or
unittest.mock.patch.
"""

import os
import pytest

# Apply env vars before any app module is imported
os.environ.setdefault("TBOX_APP_ID", "test-app-id")
os.environ.setdefault("TBOX_TOKEN", "test-token")
os.environ.setdefault("TBOX_BASE_URL", "https://api.tbox.cn")
os.environ.setdefault("ADAPTER_MODEL_ID", "tbox-codex")
os.environ.setdefault("API_KEYS", "test-key")


from httpx import AsyncClient, ASGITransport

from app.main import app
from app.core.config import get_settings


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
async def async_client():
    """Async test client that hits the FastAPI app directly (no real network)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client
