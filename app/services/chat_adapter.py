"""
Chat adaptation layer: OpenAI <-> TBox translation.

This module owns:
  1. Extracting the user query from OpenAI messages
  2. Building a TBoxChatRequest from a ChatCompletionRequest
  3. Calling tbox_client and converting the response to OpenAI shapes
  4. Maintaining the session (conversationId) across both streaming and
     non-streaming flows
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import AsyncIterator

from app.core.config import Settings
from app.schemas.openai import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessageResponse,
    Choice,
    DeltaContent,
    StreamChoice,
    Usage,
)
from app.schemas.tbox import TBoxChatRequest, TBoxFile
from app.services import tbox_client
from app.stores import session_store
from app.utils.errors import TBoxUpstreamError
from app.utils.sse import (
    SSE_DONE,
    format_sse_data,
    iter_sse_lines,
    parse_tbox_sse_line,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_query(request: ChatCompletionRequest) -> str:
    """
    Return the content of the last user-role message.

    Raises ValueError if no user message is found.
    """
    for message in reversed(request.messages):
        if message.role == "user":
            return message.content
    raise ValueError("No user message found in messages list")


def _extract_system_prompt(request: ChatCompletionRequest) -> str | None:
    """
    Concatenate all system-role messages into a single system prompt.

    Returns None if no system messages are present.
    """
    system_parts = [m.content for m in request.messages if m.role in {"system", "developer"}]
    if not system_parts:
        return None
    return "\n".join(system_parts)


async def _build_tbox_request(
    request: ChatCompletionRequest,
    settings: Settings,
    stream: bool = False,
) -> TBoxChatRequest:
    """Translate a ChatCompletionRequest into a TBoxChatRequest."""
    user = request.user or settings.adapter_default_user
    query = _extract_query(request)
    system_prompt = _extract_system_prompt(request)
    conversation_id = await session_store.aget_conversation_id(user)

    # Convert file references to TBox format
    tbox_files: list[TBoxFile] | None = None
    if request.files:
        tbox_files = [
            TBoxFile(type=f.type, fileId=f.file_id) for f in request.files
        ]

    return TBoxChatRequest(
        appId=settings.tbox_app_id,
        query=query,
        userId=user,
        conversationId=conversation_id,
        stream=stream,
        systemPrompt=system_prompt,
        files=tbox_files,
    )


# ---------------------------------------------------------------------------
# Non-streaming flow
# ---------------------------------------------------------------------------


async def handle_chat_once(
    request: ChatCompletionRequest,
    settings: Settings,
) -> ChatCompletionResponse:
    """
    Call TBox in non-streaming mode and return an OpenAI ChatCompletionResponse.
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
    user = request.user or settings.adapter_default_user
    if conv_id:
        await session_store.aset_conversation_id(user, conv_id)

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    return ChatCompletionResponse(
        id=completion_id,
        model=settings.adapter_model_id,
        created=int(time.time()),
        choices=[
            Choice(
                index=0,
                message=ChatMessageResponse(role="assistant", content=answer_text),
                finish_reason="stop",
            )
        ],
        usage=Usage(),  # TBox doesn't expose token counts in v1
    )


# ---------------------------------------------------------------------------
# Streaming flow
# ---------------------------------------------------------------------------


async def handle_chat_stream(
    request: ChatCompletionRequest,
    settings: Settings,
) -> AsyncIterator[str]:
    """
    Async generator that yields OpenAI-compatible SSE lines.

    Internally:
      1. Calls TBox with stream=True
      2. Parses TBox SSE events line-by-line
      3. Emits the first chunk with role="assistant"
      4. Forwards chunk/text deltas
      5. Suppresses meta/thinking events (logs them)
      6. Extracts conversationId from header event
      7. Emits [DONE] at the end
    """
    tbox_req = await _build_tbox_request(request, settings, stream=True)
    user = request.user or settings.adapter_default_user
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    model_id = settings.adapter_model_id
    created = int(time.time())
    first_chunk = True  # used to emit the role field once

    def make_chunk(content: str | None = None, finish_reason: str | None = None, role: str | None = None) -> str:
        delta = DeltaContent(role=role, content=content)
        chunk = ChatCompletionChunk(
            id=completion_id,
            created=created,
            model=model_id,
            choices=[StreamChoice(index=0, delta=delta, finish_reason=finish_reason)],
        )
        return format_sse_data(chunk.model_dump(exclude_none=True))

    try:
        async with tbox_client.chat_stream(tbox_req) as byte_iter:
            async for raw_line in iter_sse_lines(byte_iter):
                event_data = parse_tbox_sse_line(raw_line)
                if event_data is None:
                    continue

                event_type: str = event_data.get("event", "")
                payload: dict = event_data.get("payload") or {}

                if event_type == "header":
                    # TBox sends conversationId in the header event
                    conv_id = payload.get("conversationId")
                    if conv_id:
                        await session_store.aset_conversation_id(user, conv_id)
                    # Emit the opening chunk with role set
                    yield make_chunk(role="assistant", content="")
                    first_chunk = False

                elif event_type == "chunk":
                    text: str = payload.get("text", "")
                    if first_chunk:
                        # Guard: emit role chunk if header was absent
                        yield make_chunk(role="assistant", content="")
                        first_chunk = False
                    if text:
                        yield make_chunk(content=text)

                elif event_type == "meta":
                    # Contains usage/timing info — log only, not forwarded in v1
                    logger.debug("TBox meta event: %s", payload)
                    # TODO: extract token counts here when TBox exposes them

                elif event_type == "thinking":
                    # Internal chain-of-thought — not forwarded in v1
                    logger.debug("TBox thinking event (suppressed): %s", payload)
                    # TODO: optionally surface as a separate delta type

                elif event_type == "error":
                    error_msg = payload.get("message", "Unknown TBox error")
                    logger.error("TBox error event: %s", error_msg)
                    # Send error as an SSE event so clients can detect it
                    yield format_sse_data({
                        "error": {
                            "message": error_msg,
                            "type": "upstream_error",
                            "code": None,
                        }
                    })
                    yield SSE_DONE
                    return

                else:
                    logger.debug("TBox unknown event type=%r payload=%s", event_type, payload)

    except TBoxUpstreamError as exc:
        logger.error("TBox upstream error during stream: %s", exc)
        yield format_sse_data({
            "error": {
                "message": str(exc),
                "type": "upstream_error",
                "code": None,
            }
        })
        yield SSE_DONE
        return
    except Exception as exc:
        logger.exception("Unexpected error during stream")
        yield format_sse_data({
            "error": {
                "message": f"Internal server error: {exc}",
                "type": "internal_server_error",
                "code": None,
            }
        })
        yield SSE_DONE
        return

    # Emit the stop chunk then [DONE]
    yield make_chunk(finish_reason="stop")
    yield SSE_DONE
