"""
Pydantic models matching the OpenAI Chat Completions API shapes.

Fields marked "ignored in v1" are accepted by the schema for compatibility
but not forwarded to TBox in this release.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class ChatFileRef(BaseModel):
    """File reference for attaching uploaded files to a chat request.

    Use the fileId returned by POST /v1/files.
    """

    type: str  # e.g. "IMAGE", "AUDIO", "VIDEO", "FILE"
    file_id: str = Field(..., alias="fileId")

    model_config = {"populate_by_name": True}


class ChatMessage(BaseModel):
    """A single message in the conversation history."""

    role: Literal["system", "developer", "user", "assistant", "tool"]
    content: str
    # name / tool_call_id — ignored in v1, kept for schema compatibility
    name: Optional[str] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible POST /v1/chat/completions request body."""

    model: str
    messages: list[ChatMessage]
    stream: bool = False

    # user is used as the session key (maps to TBox userId)
    user: Optional[str] = None

    # File attachments — pass fileIds obtained from POST /v1/files
    files: Optional[list[ChatFileRef]] = None

    # --- fields ignored in v1, accepted to avoid client-side errors ---
    temperature: Optional[float] = None       # ignored
    top_p: Optional[float] = None             # ignored
    max_tokens: Optional[int] = None          # ignored
    n: Optional[int] = None                   # ignored (always 1)
    stop: Optional[Any] = None                # ignored
    presence_penalty: Optional[float] = None  # ignored
    frequency_penalty: Optional[float] = None # ignored
    logit_bias: Optional[dict] = None         # ignored
    tools: Optional[list] = None              # ignored — function calling not in v1
    tool_choice: Optional[Any] = None         # ignored — function calling not in v1
    response_format: Optional[Any] = None     # ignored


# ---------------------------------------------------------------------------
# Non-stream response models
# ---------------------------------------------------------------------------


class ChatMessageResponse(BaseModel):
    role: str = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ChatMessageResponse
    finish_reason: str = "stop"


class Usage(BaseModel):
    """Token counts — TBox doesn't provide these; returning zeros for compat."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    """OpenAI-compatible non-streaming response."""

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


# ---------------------------------------------------------------------------
# Streaming response models (SSE chunks)
# ---------------------------------------------------------------------------


class DeltaContent(BaseModel):
    """Incremental content delta inside a streaming chunk."""

    role: Optional[str] = None
    content: Optional[str] = None


class StreamChoice(BaseModel):
    index: int = 0
    delta: DeltaContent
    finish_reason: Optional[str] = None


class ChatCompletionChunk(BaseModel):
    """OpenAI-compatible streaming chunk."""

    id: str
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[StreamChoice]


# ---------------------------------------------------------------------------
# Models list response
# ---------------------------------------------------------------------------


class ModelCard(BaseModel):
    id: str
    object: str = "model"
    created: int = Field(default_factory=lambda: int(time.time()))
    owned_by: str = "tbox"


class ModelList(BaseModel):
    object: str = "list"
    data: list[ModelCard]
