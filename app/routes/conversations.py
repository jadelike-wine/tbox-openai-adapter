"""
Conversation management routes.

Endpoints:
  POST /v1/conversations              — Create a new conversation
  GET  /v1/conversations              — List conversations
  GET  /v1/conversations/{id}/messages — List messages in a conversation

These routes are mounted under both /openai and the root / prefix in main.py.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.schemas.tbox import (
    TBoxConversationListResponse,
    TBoxCreateConversationResponse,
    TBoxMessageListResponse,
)
from app.services import tbox_client
from app.utils.errors import TBoxUpstreamError, openai_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/conversations", tags=["conversations"])


# ---------------------------------------------------------------------------
# POST /v1/conversations — Create conversation
# ---------------------------------------------------------------------------


@router.post("", summary="Create a new conversation")
async def create_conversation(
    settings: Settings = Depends(get_settings),
):
    """
    Create a new TBox conversation for the configured appId.

    Returns the new conversationId in the `data` field.
    """
    try:
        raw = await tbox_client.create_conversation(settings.tbox_app_id)
        return JSONResponse(content=raw)
    except TBoxUpstreamError as exc:
        logger.error("TBox create conversation error: %s", exc)
        return openai_error(str(exc), error_type="upstream_error", status_code=exc.status_code)
    except Exception as exc:
        logger.exception("Unexpected error in create_conversation")
        return openai_error(str(exc), status_code=500)


# ---------------------------------------------------------------------------
# GET /v1/conversations — List conversations
# ---------------------------------------------------------------------------


@router.get("", summary="List conversations", response_model=TBoxConversationListResponse)
async def list_conversations(
    user_id: Optional[str] = Query(None, alias="userId", description="Filter by user ID"),
    source: Optional[Literal["AGENT_SDK", "OPENAPI", "IOT_SDK"]] = Query(
        None, description="Filter by channel"
    ),
    page_num: int = Query(1, alias="pageNum", ge=1, description="Page number, starts from 1"),
    page_size: int = Query(10, alias="pageSize", ge=1, le=50, description="Page size, max 50"),
    sort_order: Literal["ASC", "DESC"] = Query(
        "DESC", alias="sortOrder", description="Sort by createAt"
    ),
    settings: Settings = Depends(get_settings),
):
    """
    Query conversations initiated via the TBox OpenAPI or SDK.

    - `userId` — filter to a specific user; omit to return all users
    - `source`  — filter by channel: AGENT_SDK | OPENAPI | IOT_SDK
    - `pageNum` / `pageSize` — pagination (pageSize max 50)
    - `sortOrder` — ASC or DESC by creation time
    """
    try:
        raw = await tbox_client.list_conversations(
            app_id=settings.tbox_app_id,
            user_id=user_id,
            source=source,
            page_num=page_num,
            page_size=page_size,
            sort_order=sort_order,
        )
        return JSONResponse(content=raw)
    except TBoxUpstreamError as exc:
        logger.error("TBox list conversations error: %s", exc)
        return openai_error(str(exc), error_type="upstream_error", status_code=exc.status_code)
    except Exception as exc:
        logger.exception("Unexpected error in list_conversations")
        return openai_error(str(exc), status_code=500)


# ---------------------------------------------------------------------------
# GET /v1/conversations/{conversation_id}/messages — List messages
# ---------------------------------------------------------------------------


@router.get(
    "/{conversation_id}/messages",
    summary="List messages in a conversation",
    response_model=TBoxMessageListResponse,
)
async def list_messages(
    conversation_id: str,
    page_num: int = Query(1, alias="pageNum", ge=1, description="Page number, starts from 1"),
    page_size: int = Query(10, alias="pageSize", ge=1, le=50, description="Page size, max 50"),
    sort_order: Literal["ASC", "DESC"] = Query(
        "DESC", alias="sortOrder", description="Sort by createAt"
    ),
    settings: Settings = Depends(get_settings),
):
    """
    Query the messages (Q&A rounds) in a specific conversation.

    - `conversation_id` — the conversation to query (path parameter)
    - `pageNum` / `pageSize` — pagination (pageSize max 50)
    - `sortOrder` — ASC or DESC by creation time
    """
    try:
        raw = await tbox_client.list_messages(
            conversation_id=conversation_id,
            page_num=page_num,
            page_size=page_size,
            sort_order=sort_order,
        )
        return JSONResponse(content=raw)
    except TBoxUpstreamError as exc:
        logger.error("TBox list messages error: %s", exc)
        return openai_error(str(exc), error_type="upstream_error", status_code=exc.status_code)
    except Exception as exc:
        logger.exception("Unexpected error in list_messages")
        return openai_error(str(exc), status_code=500)
