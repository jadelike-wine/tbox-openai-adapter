"""
POST /v1/messages  (Anthropic Claude Messages API)

Handles both streaming (SSE) and non-streaming responses.
All heavy lifting is delegated to the anthropic_adapter service.

This router is mounted under the /anthropic prefix in main.py, so the
full endpoint path is:
  POST http://localhost:2233/anthropic/v1/messages
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import Settings, get_settings
from app.schemas.anthropic import AnthropicMessagesRequest, AnthropicMessagesResponse
from app.services import anthropic_adapter
from app.utils.errors import TBoxUpstreamError, openai_error
from app.utils.metrics import CHAT_REQUESTS_TOTAL, CHAT_REQUEST_ERRORS_TOTAL

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/messages", response_model=AnthropicMessagesResponse)
async def create_message(
    request: AnthropicMessagesRequest,
    settings: Settings = Depends(get_settings),
):
    """
    Anthropic-compatible Messages endpoint.

    - stream=false  → returns a JSON MessagesResponse
    - stream=true   → returns an SSE StreamingResponse following the
                      Anthropic streaming event protocol
    """
    user = (request.metadata or {}).get("user_id", "") if hasattr(request, "metadata") else ""
    user = user or settings.adapter_default_user
    model = request.model
    stream_label = str(request.stream).lower()

    CHAT_REQUESTS_TOTAL.labels(
        model=model, user=user, stream=stream_label, api_format="anthropic"
    ).inc()

    try:
        if request.stream:
            return _streaming_response(request, settings)
        else:
            return await _json_response(request, settings)
    except ValueError as exc:
        logger.warning("Bad request: %s", exc)
        CHAT_REQUEST_ERRORS_TOTAL.labels(
            model=model, user=user, error_type="invalid_request_error", api_format="anthropic"
        ).inc()
        return openai_error(str(exc), error_type="invalid_request_error", status_code=400)
    except TBoxUpstreamError as exc:
        logger.error("TBox upstream error: %s", exc)
        CHAT_REQUEST_ERRORS_TOTAL.labels(
            model=model, user=user, error_type="upstream_error", api_format="anthropic"
        ).inc()
        return openai_error(str(exc), error_type="upstream_error", status_code=exc.status_code)
    except Exception as exc:
        logger.exception("Unexpected error in create_message")
        CHAT_REQUEST_ERRORS_TOTAL.labels(
            model=model, user=user, error_type="internal_error", api_format="anthropic"
        ).inc()
        return openai_error(str(exc), status_code=500)


async def _json_response(
    request: AnthropicMessagesRequest,
    settings: Settings,
) -> JSONResponse:
    """Non-streaming path: await the full TBox response then return JSON."""
    result: AnthropicMessagesResponse = await anthropic_adapter.handle_messages_once(
        request, settings
    )
    return JSONResponse(content=result.model_dump())


def _streaming_response(
    request: AnthropicMessagesRequest,
    settings: Settings,
) -> StreamingResponse:
    """
    Streaming path: wrap the async generator in a StreamingResponse.

    Anthropic uses text/event-stream with named events
    (message_start, content_block_delta, etc.).
    """
    generator = anthropic_adapter.handle_messages_stream(request, settings)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
