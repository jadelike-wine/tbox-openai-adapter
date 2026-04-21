"""
Pydantic models for TBox API request and response shapes.

These are internal models — never exposed directly to OpenAI clients.
"""

from __future__ import annotations

from typing import Any, Literal, Optional
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Chat request / response
# ---------------------------------------------------------------------------


class TBoxFile(BaseModel):
    """File reference passed in chat requests after upload."""

    type: str        # e.g. "IMAGE", "AUDIO", "VIDEO", "FILE"
    fileId: str


class TBoxChatRequest(BaseModel):
    """Request body sent to POST /api/chat."""

    appId: str
    query: str
    userId: str
    conversationId: Optional[str] = None  # omit on first turn
    stream: bool = False
    files: Optional[list[TBoxFile]] = None  # file attachments
    systemPrompt: Optional[str] = None  # system-level instruction


# ---------------------------------------------------------------------------
# Response events (streaming)
# ---------------------------------------------------------------------------


class TBoxEvent(BaseModel):
    """
    Generic envelope for a single SSE event emitted by TBox.

    TBox event types:
      - header  : session metadata (conversationId lives here)
      - chunk   : incremental text token
      - meta    : final metadata (usage, timing) — logged but not forwarded
      - thinking: internal CoT token — logged but not forwarded
      - error   : upstream error payload
    """

    event: str
    payload: Optional[dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Response (non-streaming)
# ---------------------------------------------------------------------------


class TBoxChatResponse(BaseModel):
    """
    Shape of a non-streaming TBox chat response.

    TBox may vary its response shape; we capture what we need and ignore the rest.
    """

    conversationId: Optional[str] = None
    text: Optional[str] = None          # assembled answer text
    answer: Optional[str] = None        # alternative field name used by some flows
    # Additional fields are ignored in v1
    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Conversation models
# ---------------------------------------------------------------------------


class TBoxConversation(BaseModel):
    """A single conversation entry returned by the list endpoint."""

    conversationId: str
    userId: str
    source: str                  # AGENT_SDK | OPENAPI | IOT_SDK
    createAt: int                # Unix timestamp (seconds)


class TBoxConversationListData(BaseModel):
    conversations: list[TBoxConversation]
    currentPage: int
    pageSize: int
    total: int


class TBoxConversationListResponse(BaseModel):
    errorCode: str
    errorMsg: str
    data: Optional[TBoxConversationListData] = None
    traceId: Optional[str] = None


class TBoxCreateConversationResponse(BaseModel):
    errorCode: str
    errorMsg: str
    data: Optional[str] = None   # the new conversationId
    traceId: Optional[str] = None


# ---------------------------------------------------------------------------
# Message models
# ---------------------------------------------------------------------------


class TBoxAnswer(BaseModel):
    """One answer block inside a message."""

    lane: str = "default"
    mediaType: str               # text | image
    text: Optional[str] = None
    url: Optional[list[str]] = None
    expireAt: Optional[int] = None


class TBoxMessageFile(BaseModel):
    """File attachment referenced in a message."""

    type: str                    # IMAGE | AUDIO | VIDEO | FILE
    url: str
    expireAt: Optional[int] = None


class TBoxMessage(BaseModel):
    """A single message (one Q&A round) returned by the messages endpoint."""

    messageId: str
    conversationId: str
    appId: str
    query: Optional[str] = None
    answers: list[TBoxAnswer] = []
    files: list[TBoxMessageFile] = []
    createAt: Optional[int] = None
    updateAt: Optional[int] = None
    status: Optional[str] = None  # SUCCESS | ERROR | BLOCK | PENDING


class TBoxMessageListData(BaseModel):
    messages: list[TBoxMessage]
    currentPage: int
    pageSize: int
    total: int


class TBoxMessageListResponse(BaseModel):
    errorCode: str
    errorMsg: str
    data: Optional[TBoxMessageListData] = None
    traceId: Optional[str] = None


# ---------------------------------------------------------------------------
# File models
# ---------------------------------------------------------------------------


class TBoxFileUploadResponse(BaseModel):
    errorCode: str
    errorMsg: str
    data: Optional[str] = None   # fileId on success
    traceId: Optional[str] = None


class TBoxFileDetail(BaseModel):
    id: str
    fileName: str
    fileType: str
    bytes: Optional[int] = None
    gmtCreate: Optional[str] = None


class TBoxFileRetrieveResponse(BaseModel):
    errorCode: str
    errorMsg: str
    data: Optional[TBoxFileDetail] = None
    traceId: Optional[str] = None
