"""
Chat adaptation layer: Anthropic Messages API <-> TBox translation.

This module owns:
  1. Extracting the user query from Anthropic messages
  2. Building a TBoxChatRequest from an AnthropicMessagesRequest
  3. Calling tbox_client and converting the response to Anthropic shapes
  4. Maintaining the session (conversationId) across both streaming and
     non-streaming flows
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import AsyncIterator

from app.core.config import Settings
from app.schemas.anthropic import (
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    AnthropicResponseContent,
    AnthropicStreamContentBlockDelta,
    AnthropicStreamContentBlockStart,
    AnthropicStreamContentBlockStop,
    AnthropicStreamDelta,
    AnthropicStreamMessageDelta,
    AnthropicStreamMessageStart,
    AnthropicStreamMessageStop,
    AnthropicUsage,
)
from app.schemas.tbox import TBoxChatRequest
from app.schemas.tbox import TBoxFile
from app.services import tbox_client
from app.stores import session_store
from app.utils.errors import TBoxUpstreamError
from app.utils.sse import (
    SSE_DONE,
    iter_sse_lines,
    parse_tbox_sse_line,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_query(request: AnthropicMessagesRequest) -> str:
    """
    Return the text content of the last user-role message.

    Handles both plain-string content and typed content blocks.
    Raises ValueError if no user message is found.
    """
    for message in reversed(request.messages):
        if message.role == "user":
            content = message.content
            if isinstance(content, str):
                return content
            # Extract text from typed content blocks
            texts = [block.text for block in content if hasattr(block, "text")]
            return " ".join(texts) if texts else ""
    raise ValueError("No user message found in messages list")


def _get_user(request: AnthropicMessagesRequest, settings: Settings) -> str:
    """Extract user identifier from metadata or fall back to default."""
    if request.metadata and request.metadata.get("user_id"):
        return request.metadata["user_id"]
    return settings.adapter_default_user


async def _build_tbox_request(
    request: AnthropicMessagesRequest,
    settings: Settings,
    stream: bool = False,
) -> TBoxChatRequest:
    """Translate an AnthropicMessagesRequest into a TBoxChatRequest."""
    user = _get_user(request, settings)
    query = _extract_query(request)
    conversation_id = await session_store.aget_conversation_id(user)
    tbox_files = _extract_files(request)

    return TBoxChatRequest(
        appId=settings.tbox_app_id,
        query=query,
        userId=user,
        conversationId=conversation_id,
        stream=stream,
        systemPrompt=request.system if request.system else None,
        files=tbox_files,
    )


def _extract_files(request: AnthropicMessagesRequest) -> list[TBoxFile] | None:
    files: list[TBoxFile] = []
    for message in request.messages:
        if message.role != "user" or isinstance(message.content, str):
            continue
        for block in message.content:
            if getattr(block, "type", None) != "file":
                continue
            source = getattr(block, "source", None)
            file_id = getattr(source, "file_id", None)
            if file_id:
                file_type = getattr(block, "file_kind", "FILE")
                files.append(TBoxFile(type=file_type, fileId=file_id))
    return files or None


def _format_anthropic_sse(event_type: str, payload: dict) -> str:
    """Format an Anthropic SSE event: 'event: <type>\\ndata: <json>\\n\\n'."""
    return f"event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------------------------
# Non-streaming flow
# ---------------------------------------------------------------------------


async def handle_messages_once(
    request: AnthropicMessagesRequest,
    settings: Settings,
) -> AnthropicMessagesResponse:
    """
    Call TBox in non-streaming mode and return an Anthropic MessagesResponse.
    """
    tbox_req = await _build_tbox_request(request, settings, stream=False)
    raw: dict = await tbox_client.chat_once(tbox_req)

    logger.debug("TBox non-stream raw response: %s", raw)

    # Extract answer text — TBox may use different field names
    answer_text: str = (
        raw.get("text")
        or raw.get("answer")
        or raw.get("data", {}).get("text")
        or raw.get("data", {}).get("answer")
        or ""
    )

    # Update session store if conversationId is present in the response
    conv_id: str | None = (
        raw.get("conversationId")
        or raw.get("data", {}).get("conversationId")
    )
    user = _get_user(request, settings)
    if conv_id:
        await session_store.aset_conversation_id(user, conv_id)

    return AnthropicMessagesResponse(
        id=f"msg_{uuid.uuid4().hex}",
        model=settings.adapter_model_id,
        content=[AnthropicResponseContent(type="text", text=answer_text)],
        stop_reason="end_turn",
        usage=AnthropicUsage(),
    )


# ---------------------------------------------------------------------------
# Streaming flow
# ---------------------------------------------------------------------------


async def handle_messages_stream(
    request: AnthropicMessagesRequest,
    settings: Settings,
) -> AsyncIterator[str]:
    """
    Async generator that yields Anthropic-compatible SSE events.

    Anthropic streaming event sequence:
      1. message_start        — metadata about the message
      2. content_block_start  — opening of content block[0]
      3. content_block_delta* — incremental text deltas
      4. content_block_stop   — closing of content block[0]
      5. message_delta        — stop_reason / usage update
      6. message_stop         — end of stream
    """
    tbox_req = await _build_tbox_request(request, settings, stream=True)
    user = _get_user(request, settings)
    message_id = f"msg_{uuid.uuid4().hex}"
    model_id = settings.adapter_model_id

    # 1. Emit message_start with an empty placeholder message
    placeholder_msg = AnthropicMessagesResponse(
        id=message_id,
        model=model_id,
        content=[],
        stop_reason=None,
        usage=AnthropicUsage(),
    )
    yield _format_anthropic_sse(
        "message_start",
        AnthropicStreamMessageStart(message=placeholder_msg).model_dump(),
    )

    # 2. Emit content_block_start for block index 0
    yield _format_anthropic_sse(
        "content_block_start",
        AnthropicStreamContentBlockStart(
            index=0,
            content_block=AnthropicResponseContent(type="text", text=""),
        ).model_dump(),
    )

    # 3. Stream TBox deltas as content_block_delta events
    stream_error = False

    try:
        async with tbox_client.chat_stream(tbox_req) as byte_iter:
            async for raw_line in iter_sse_lines(byte_iter):
                event_data = parse_tbox_sse_line(raw_line)
                if event_data is None:
                    continue

                event_type: str = event_data.get("event", "")
                payload: dict = event_data.get("payload") or {}

                if event_type == "header":
                    conv_id = payload.get("conversationId")
                    if conv_id:
                        await session_store.aset_conversation_id(user, conv_id)

                elif event_type == "chunk":
                    text: str = payload.get("text", "")
                    if text:
                        yield _format_anthropic_sse(
                            "content_block_delta",
                            AnthropicStreamContentBlockDelta(
                                index=0,
                                delta=AnthropicStreamDelta(type="text_delta", text=text),
                            ).model_dump(),
                        )

                elif event_type == "meta":
                    logger.debug("TBox meta event: %s", payload)

                elif event_type == "thinking":
                    logger.debug("TBox thinking event (suppressed): %s", payload)

                elif event_type == "error":
                    error_msg = payload.get("message", "Unknown TBox error")
                    logger.error("TBox error event: %s", error_msg)
                    yield _format_anthropic_sse("error", {
                        "type": "error",
                        "error": {"type": "upstream_error", "message": error_msg},
                    })
                    yield _format_anthropic_sse(
                        "message_stop",
                        AnthropicStreamMessageStop().model_dump(),
                    )
                    yield SSE_DONE
                    stream_error = True
                    break

                else:
                    logger.debug("TBox unknown event type=%r payload=%s", event_type, payload)

    except (TBoxUpstreamError, Exception) as exc:
        is_upstream = isinstance(exc, TBoxUpstreamError)
        error_type = "upstream_error" if is_upstream else "internal_server_error"
        log_fn = logger.error if is_upstream else logger.exception
        log_fn("Error during Anthropic stream: %s", exc)
        yield _format_anthropic_sse("error", {
            "type": "error",
            "error": {"type": error_type, "message": str(exc)},
        })
        yield _format_anthropic_sse(
            "message_stop",
            AnthropicStreamMessageStop().model_dump(),
        )
        yield SSE_DONE
        stream_error = True

    if stream_error:
        return

    # 4. Emit content_block_stop
    yield _format_anthropic_sse(
        "content_block_stop",
        AnthropicStreamContentBlockStop(index=0).model_dump(),
    )

    # 5. Emit message_delta with stop_reason
    yield _format_anthropic_sse(
        "message_delta",
        AnthropicStreamMessageDelta(
            delta={"stop_reason": "end_turn", "stop_sequence": None},
            usage=AnthropicUsage(),
        ).model_dump(),
    )

    # 6. Emit message_stop
    yield _format_anthropic_sse(
        "message_stop",
        AnthropicStreamMessageStop().model_dump(),
    )
    yield SSE_DONE
