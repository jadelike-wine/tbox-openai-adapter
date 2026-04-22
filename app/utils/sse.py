"""
SSE (Server-Sent Events) helpers for streaming responses.

Provides:
  - format_sse_chunk : serialise a ChatCompletionChunk to a data: line
  - SSE_DONE         : the terminal SSE event
  - parse_tbox_sse_event: parse one full SSE event from TBox's stream
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator, Optional

logger = logging.getLogger(__name__)

# The SSE termination sentinel required by the OpenAI spec
SSE_DONE = "data: [DONE]\n\n"


def format_sse_data(payload: dict) -> str:
    """Serialise *payload* as a single SSE data line."""
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def parse_tbox_sse_event(raw_event: str) -> Optional[dict]:
    """
    Parse a full SSE event block from TBox into a dict.

    Supports both payload styles:
      1) data: {"event":"chunk","payload":{"text":"..."}}
      2) event: message
         data: {"type":"chunk","payload":"{\"text\":\"...\"}"}

    Returns:
      {"event": <event_type>, "payload": <dict>}

    Returns None if the event should be skipped.
    """
    raw = raw_event.strip()
    if not raw:
        return None

    sse_event_name = ""
    data_lines: list[str] = []
    for line in raw.splitlines():
        line = line.rstrip("\r")
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            sse_event_name = line[len("event:"):].strip()
            continue
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].strip())
            continue

    data_part = "\n".join(data_lines).strip()
    if not data_part or data_part == "[DONE]":
        return None

    try:
        parsed = json.loads(data_part)
    except json.JSONDecodeError:
        logger.warning("Could not parse TBox SSE event: %r", raw_event)
        return None

    # Old style: data already contains {"event": "...", "payload": {...}}
    if "event" in parsed:
        payload = parsed.get("payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                payload = {}
        return {"event": parsed.get("event", ""), "payload": payload}

    # New style: SSE event + data {"type":"chunk","payload":"{...}"}
    event_type = parsed.get("type") or sse_event_name
    payload = parsed.get("payload", {})
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if payload is None:
        payload = {}
    return {"event": event_type or "", "payload": payload}


async def iter_sse_events(response_aiter: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """
    Yield full SSE event blocks from a chunked byte stream.
    """
    buffer = ""
    async for chunk in response_aiter:
        logger.debug("Received raw chunk from TBox: %r", chunk)
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n\n" in buffer:
            event, buffer = buffer.split("\n\n", 1)
            logger.debug("Parsed SSE event: %r", event)
            yield event
    if buffer.strip():
        logger.debug("Final SSE buffer: %r", buffer)
        yield buffer
