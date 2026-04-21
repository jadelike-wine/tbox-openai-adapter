"""
File management routes.

Endpoints:
  POST /v1/files              — Upload a file to TBox
  GET  /v1/files/{file_id}    — Retrieve file details by fileId

These routes are mounted under both /openai and the root / prefix in main.py.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse

from app.core.config import Settings, get_settings
from app.schemas.tbox import TBoxFileRetrieveResponse, TBoxFileUploadResponse
from app.services import tbox_client
from app.utils.errors import TBoxUpstreamError, openai_error

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/files", tags=["files"])


# ---------------------------------------------------------------------------
# POST /v1/files — Upload file
# ---------------------------------------------------------------------------


@router.post("", summary="Upload a file to TBox")
async def upload_file(
    file: UploadFile = File(..., description="File to upload"),
    settings: Settings = Depends(get_settings),
):
    """
    Upload a file to TBox for use in multimodal conversations or knowledge bases.

    - Supported types: text, image, audio, video, document, etc.
    - File validity: 3 months; expired files are automatically deleted by TBox.
    - Returns a `fileId` that can be passed in the `files` field of chat requests.

    **Request:** `multipart/form-data` with a `file` field.
    """
    try:
        file_bytes = await file.read()
        content_type = file.content_type or "application/octet-stream"
        filename = file.filename or "upload"

        logger.info(
            "File upload request: name=%s content_type=%s size=%d",
            filename,
            content_type,
            len(file_bytes),
        )

        raw = await tbox_client.upload_file(file_bytes, filename, content_type)
        return JSONResponse(content=raw)

    except TBoxUpstreamError as exc:
        logger.error("TBox file upload error: %s", exc)
        return openai_error(str(exc), error_type="upstream_error", status_code=exc.status_code)
    except Exception as exc:
        logger.exception("Unexpected error in upload_file")
        return openai_error(str(exc), status_code=500)


# ---------------------------------------------------------------------------
# GET /v1/files/{file_id} — Retrieve file details
# ---------------------------------------------------------------------------


@router.get(
    "/{file_id}",
    summary="Retrieve file details",
    response_model=TBoxFileRetrieveResponse,
)
async def retrieve_file(
    file_id: str,
    settings: Settings = Depends(get_settings),
):
    """
    Retrieve details for a previously uploaded file.

    - `file_id` — the fileId returned by the upload endpoint (path parameter)

    Returns file name, type, size (bytes), and creation time.
    """
    try:
        raw = await tbox_client.retrieve_file(file_id)
        return JSONResponse(content=raw)
    except TBoxUpstreamError as exc:
        logger.error("TBox file retrieve error: %s", exc)
        return openai_error(str(exc), error_type="upstream_error", status_code=exc.status_code)
    except Exception as exc:
        logger.exception("Unexpected error in retrieve_file")
        return openai_error(str(exc), status_code=500)
