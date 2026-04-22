"""
API Key authentication middleware.

Validates the Authorization header on all requests except /health and /docs.
When API_KEYS is empty in config, authentication is disabled (open access).

Usage:
  Set API_KEYS="key1,key2,key3" in .env to enable authentication.
  Clients must send: Authorization: Bearer <key>
"""

from __future__ import annotations

import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Paths that never require authentication
_PUBLIC_PATHS = {"/", "/health", "/metrics", "/docs", "/redoc", "/openapi.json", "/playground"}
_PUBLIC_PREFIXES = ("/playground-static/",)


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware that validates Bearer token against configured API_KEYS.

    If API_KEYS is empty, all requests are allowed (backward compatible).
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        settings = get_settings()
        self._valid_keys = settings.api_keys_set
        self._required = settings.auth_required
        self._enabled = bool(self._valid_keys)
        if self._enabled:
            logger.info("API key authentication enabled (%d key(s) configured)", len(self._valid_keys))
        elif self._required:
            raise RuntimeError(
                "AUTH_REQUIRED=true but API_KEYS is empty. "
                "Set API_KEYS or disable AUTH_REQUIRED explicitly."
            )
        else:
            logger.warning(
                "API key authentication is DISABLED (AUTH_REQUIRED=false)"
            )

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> JSONResponse:
        # Skip auth for public paths
        if request.url.path in _PUBLIC_PATHS or request.url.path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)

        # Skip auth if not enabled
        if not self._enabled:
            return await call_next(request)

        # Extract and validate Bearer token
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return _auth_error("Missing or malformed Authorization header. Expected: Bearer <api_key>")

        token = auth_header[len("Bearer "):].strip()
        if token not in self._valid_keys:
            return _auth_error("Invalid API key")

        return await call_next(request)


def _auth_error(message: str) -> JSONResponse:
    """Return an OpenAI-compatible 401 error response."""
    return JSONResponse(
        status_code=401,
        content={
            "error": {
                "message": message,
                "type": "authentication_error",
                "code": "invalid_api_key",
            }
        },
    )
