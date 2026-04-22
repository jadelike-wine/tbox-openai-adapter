"""
Pydantic models matching the Anthropic Claude Messages API shapes.

Reference: https://docs.anthropic.com/en/api/messages
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class AnthropicTextContent(BaseModel):
    """Text content block in an Anthropic message."""

    type: Literal["text"]
    text: str


class AnthropicImageSource(BaseModel):
    type: Literal["base64", "url"]
    media_type: Optional[str] = None
    data: Optional[str] = None
    url: Optional[str] = None


class AnthropicImageContent(BaseModel):
    """Image content block in an Anthropic message."""

    type: Literal["image"]
    source: AnthropicImageSource


class AnthropicFileSource(BaseModel):
    type: Literal["file"]
    file_id: str


class AnthropicFileContent(BaseModel):
    """File content block that references an uploaded file id."""

    type: Literal["file"]
    source: AnthropicFileSource
    file_kind: str = "FILE"


# Content can be a plain string or a list of typed blocks
AnthropicContent = Union[
    str,
    list[Union[AnthropicTextContent, AnthropicImageContent, AnthropicFileContent]],
]


class AnthropicMessage(BaseModel):
    """A single message in the Anthropic conversation."""

    role: Literal["user", "assistant"]
    content: AnthropicContent


class AnthropicMessagesRequest(BaseModel):
    """Anthropic-compatible POST /v1/messages request body."""

    model: str
    messages: list[AnthropicMessage]
    max_tokens: int = 1024
    stream: bool = False

    # Optional system prompt: supports both plain string and typed text blocks
    system: Optional[Union[str, list[AnthropicTextContent]]] = None

    # user is used as the session key (maps to TBox userId)
    # Anthropic uses metadata.user_id for this purpose
    metadata: Optional[dict] = None

    # --- fields ignored in v1, accepted to avoid client-side errors ---
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[list[str]] = None
    tools: Optional[list] = None
    tool_choice: Optional[Any] = None


# ---------------------------------------------------------------------------
# Non-stream response models
# ---------------------------------------------------------------------------


class AnthropicResponseContent(BaseModel):
    """A single content block in the Anthropic response."""

    type: Literal["text"] = "text"
    text: str


class AnthropicUsage(BaseModel):
    """Token usage — TBox doesn't provide these; returning zeros for compat."""

    input_tokens: int = 0
    output_tokens: int = 0


class AnthropicMessagesResponse(BaseModel):
    """Anthropic-compatible non-streaming response."""

    id: str = Field(default_factory=lambda: f"msg_{uuid.uuid4().hex}")
    type: Literal["message"] = "message"
    role: Literal["assistant"] = "assistant"
    content: list[AnthropicResponseContent]
    model: str
    stop_reason: Optional[Literal["end_turn", "max_tokens", "stop_sequence", "tool_use"]] = "end_turn"
    stop_sequence: Optional[str] = None
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)


# ---------------------------------------------------------------------------
# Streaming response models (SSE events)
# ---------------------------------------------------------------------------


class AnthropicStreamDelta(BaseModel):
    """Delta content inside a streaming content_block_delta event."""

    type: Literal["text_delta"] = "text_delta"
    text: str


class AnthropicStreamContentBlockStart(BaseModel):
    type: Literal["content_block_start"] = "content_block_start"
    index: int = 0
    content_block: AnthropicResponseContent


class AnthropicStreamContentBlockDelta(BaseModel):
    type: Literal["content_block_delta"] = "content_block_delta"
    index: int = 0
    delta: AnthropicStreamDelta


class AnthropicStreamContentBlockStop(BaseModel):
    type: Literal["content_block_stop"] = "content_block_stop"
    index: int = 0


class AnthropicStreamMessageStart(BaseModel):
    type: Literal["message_start"] = "message_start"
    message: AnthropicMessagesResponse


class AnthropicStreamMessageDelta(BaseModel):
    """Emitted when stop_reason / usage updates at end of stream."""

    type: Literal["message_delta"] = "message_delta"
    delta: dict  # {stop_reason, stop_sequence}
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)


class AnthropicStreamMessageStop(BaseModel):
    type: Literal["message_stop"] = "message_stop"
