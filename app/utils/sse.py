"""
SSE (Server-Sent Events) helpers for streaming responses.

Provides:
  - format_sse_chunk : serialise a ChatCompletionChunk to a data: line
  - SSE_DONE         : the terminal SSE event
  - parse_tbox_sse_line: parse one raw line from TBox's SSE stream
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


def parse_tbox_sse_line(raw_line: str) -> Optional[dict]:
    """
    Parse a raw SSE line from TBox into a dict.

    TBox sends lines like:
        data: {"event": "chunk", "payload": {"text": "..."}}

    Returns the parsed dict, or None if the line should be skipped.
    """
    line = raw_line.strip()
    if not line or not line.startswith("data:"):
        return None
    data_part = line[len("data:"):].strip()
    if not data_part or data_part == "[DONE]":
        return None
    try:
        return json.loads(data_part)
    except json.JSONDecodeError:
        logger.warning("Could not parse TBox SSE line: %r", raw_line)
        return None


async def iter_sse_lines(response_aiter: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """
    Yield raw text lines from a chunked byte stream.

    Handles partial-line buffering so callers always receive complete lines.
    """
    buffer = ""
    async for chunk in response_aiter:
        buffer += chunk.decode("utf-8", errors="replace")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line
    if buffer.strip():
        yield buffer
