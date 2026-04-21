"""
POST /v1/chat/completions

Handles both streaming (SSE) and non-streaming responses.
All heavy lifting is delegated to the chat_adapter service.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import Settings, get_settings
from app.schemas.openai import ChatCompletionRequest, ChatCompletionResponse
from app.services import chat_adapter
from app.utils.errors import TBoxUpstreamError, openai_error
from app.utils.metrics import CHAT_REQUESTS_TOTAL, CHAT_REQUEST_ERRORS_TOTAL

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest,
    settings: Settings = Depends(get_settings),
):
    """
    OpenAI-compatible chat completions endpoint.

    - stream=false  → returns a JSON ChatCompletionResponse
    - stream=true   → returns an SSE StreamingResponse
    """
    user = request.user or settings.adapter_default_user
    model = request.model
    stream_label = str(request.stream).lower()

    CHAT_REQUESTS_TOTAL.labels(
        model=model, user=user, stream=stream_label, api_format="openai"
    ).inc()

    try:
        if request.stream:
            return _streaming_response(request, settings)
        else:
            return await _json_response(request, settings)
    except ValueError as exc:
        # e.g. no user message in messages list
        logger.warning("Bad request: %s", exc)
        CHAT_REQUEST_ERRORS_TOTAL.labels(
            model=model, user=user, error_type="invalid_request_error", api_format="openai"
        ).inc()
        return openai_error(str(exc), error_type="invalid_request_error", status_code=400)
    except TBoxUpstreamError as exc:
        logger.error("TBox upstream error: %s", exc)
        CHAT_REQUEST_ERRORS_TOTAL.labels(
            model=model, user=user, error_type="upstream_error", api_format="openai"
        ).inc()
        return openai_error(str(exc), error_type="upstream_error", status_code=exc.status_code)
    except Exception as exc:
        logger.exception("Unexpected error in chat_completions")
        CHAT_REQUEST_ERRORS_TOTAL.labels(
            model=model, user=user, error_type="internal_error", api_format="openai"
        ).inc()
        return openai_error(str(exc), status_code=500)


async def _json_response(
    request: ChatCompletionRequest,
    settings: Settings,
) -> JSONResponse:
    """Non-streaming path: await the full TBox response then return JSON."""
    result: ChatCompletionResponse = await chat_adapter.handle_chat_once(request, settings)
    return JSONResponse(content=result.model_dump())


def _streaming_response(
    request: ChatCompletionRequest,
    settings: Settings,
) -> StreamingResponse:
    """
    Streaming path: wrap the async generator in a StreamingResponse.

    The media type must be text/event-stream for SSE compliance.
    Cache-Control and X-Accel-Buffering headers prevent proxies from
    buffering the stream.
    """
    generator = chat_adapter.handle_chat_stream(request, settings)
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
