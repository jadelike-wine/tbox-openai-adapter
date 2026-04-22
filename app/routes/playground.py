from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

router = APIRouter(tags=["playground"])

_PLAYGROUND_HTML = (
    Path(__file__).resolve().parent.parent / "web" / "static" / "playground.html"
)


@router.get("/playground", include_in_schema=False)
async def playground() -> FileResponse:
    """Serve the built-in browser playground."""
    return FileResponse(_PLAYGROUND_HTML)


# Removed root path route to avoid conflicts with API info endpoint
    # Root path now serves API information instead of playground
    # Playground is available at /playground
