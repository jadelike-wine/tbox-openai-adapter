"""
Centralised error helpers.

Returns OpenAI-compatible error response bodies so clients that parse
OpenAI error envelopes continue to work correctly.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def openai_error(
    message: str,
    error_type: str = "internal_server_error",
    code: str | None = None,
    status_code: int = 500,
) -> JSONResponse:
    """Return a JSONResponse shaped like an OpenAI API error."""
    body: dict[str, Any] = {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
        }
    }
    return JSONResponse(status_code=status_code, content=body)


# ---------------------------------------------------------------------------
# FastAPI exception handlers — register these in main.py
# ---------------------------------------------------------------------------


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Catch-all handler that wraps arbitrary exceptions in an OpenAI error body."""
    logger.exception("Unhandled exception for %s %s", request.method, request.url)
    return openai_error(
        message=str(exc),
        error_type="internal_server_error",
        status_code=500,
    )


class TBoxUpstreamError(Exception):
    """Raised when TBox returns a non-2xx response or an error event."""

    def __init__(self, message: str, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


class AdapterConfigError(Exception):
    """Raised when required configuration is missing or invalid."""
