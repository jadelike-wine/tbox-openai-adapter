"""
File upload service.

v1 status: skeleton implementation.
The upload logic calls tbox_client.upload_file() but is not yet wired
into the chat flow.  Placeholders are here so the structure is ready for v2.

Future work:
  - Accept multipart file from OpenAI-style requests
  - Detect MIME type automatically
  - Return fileId for injection into TBoxChatRequest.files
  - Support multiple files per request
"""

from __future__ import annotations

import logging

from app.services.tbox_client import upload_file

logger = logging.getLogger(__name__)


async def upload_and_get_file_id(
    file_bytes: bytes,
    filename: str,
    content_type: str = "application/octet-stream",
) -> str:
    """
    Upload *file_bytes* to TBox and return the resulting fileId.

    Raises TBoxUpstreamError if the upload fails.
    """
    logger.info("Uploading file: name=%s content_type=%s size=%d", filename, content_type, len(file_bytes))
    file_id = await upload_file(file_bytes, filename, content_type)
    logger.info("File uploaded successfully: fileId=%s", file_id)
    return file_id
