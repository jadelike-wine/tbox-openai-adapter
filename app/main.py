"""
Application entry point.

Wires together:
  - FastAPI app creation
  - Lifespan (startup / shutdown) for the shared httpx client
  - Route inclusion
  - Global exception handler registration
  - Uvicorn launch when run directly
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
import json
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.core.config import get_settings
from app.middleware.auth import ApiKeyAuthMiddleware
from app.middleware.metrics_middleware import MetricsMiddleware
from app.routes import anthropic, chat, conversations, files, models, playground
from app.services import tbox_client
from app.stores import session_store
from app.utils.errors import TBoxUpstreamError, http_exception_handler, openai_error
from app.utils.metrics import (
    ACTIVE_SESSIONS,
    generate_metrics,
    metrics_content_type,
)

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

class JsonLogFormatter(logging.Formatter):
    """Minimal JSON formatter for production-friendly structured logs."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def _configure_logging() -> None:
    settings = get_settings()
    level = logging.DEBUG if settings.debug else logging.INFO
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format.lower() == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s — %(message)s")
        )
    root.addHandler(handler)


_configure_logging()

logger = logging.getLogger(__name__)
WEB_STATIC_DIR = Path(__file__).resolve().parent / "web" / "static"


# ---------------------------------------------------------------------------
# Lifespan: manage shared httpx client
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage shared resources for the lifetime of the application.

    Startup:
      - Creates the shared TBox httpx.AsyncClient and CircuitBreaker.

    Shutdown (in order):
      1. Waits up to `shutdown_timeout` seconds for active SSE streams to
         finish sending data to their clients before closing the underlying
         httpx connection pool.  This prevents in-flight streaming responses
         from being severed mid-token when the process receives SIGTERM.
      2. Closes the session store (flushes Redis pipeline if applicable).
      3. Closes the httpx client.
    """
    settings = get_settings()
    tbox_client.create_client(settings)
    # Get local IP for logging
    import socket
    def get_local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception:
            return "127.0.0.1"

    local_ip = get_local_ip()

    logger.info(
        "tbox-openai-adapter started — model=%s port=%d\n"
        "  Local access:   http://127.0.0.1:%d\n"
        "  Network access: http://%s:%d",
        settings.adapter_model_id,
        settings.port,
        settings.port,
        local_ip,
        settings.port,
    )
    yield
    # --- Graceful shutdown sequence ---
    # close_client() internally calls drain_streams(shutdown_timeout) before
    # tearing down the connection pool, giving active SSE streams a chance to
    # finish naturally.
    await tbox_client.close_client(shutdown_timeout=settings.shutdown_timeout)
    await session_store.aclose()
    logger.info("tbox-openai-adapter shut down")


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="tbox-openai-adapter",
        description="OpenAI-compatible API adapter for TBox workflows",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Allow all origins in development; tighten in production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # API key authentication (must be added after CORS so CORS headers are still set)
    app.add_middleware(ApiKeyAuthMiddleware)

    # Prometheus metrics collection — captures all HTTP requests including
    # auth failures.  Added after ApiKeyAuth so it wraps the outer layer
    # (Starlette middleware stack is LIFO: last-added runs first).
    if settings.metrics_enabled:
        app.add_middleware(MetricsMiddleware)

    # ---------------------------------------------------------------------------
    # Register routers
    #
    # Dual-prefix strategy:
    #   /openai/v1/...     — OpenAI API format  (new canonical path)
    #   /anthropic/v1/...  — Anthropic Messages API format
    #   /v1/...            — legacy path, kept for backward compatibility
    # ---------------------------------------------------------------------------

    # OpenAI format — mounted under /openai
    app.include_router(models.router, prefix="/openai")
    app.include_router(chat.router, prefix="/openai")
    app.include_router(conversations.router, prefix="/openai")
    app.include_router(files.router, prefix="/openai")

    # Anthropic format — mounted under /anthropic
    app.include_router(anthropic.router, prefix="/anthropic")

    # Legacy /v1 routes — backward compatibility (no prefix)
    app.include_router(models.router)
    app.include_router(chat.router)
    app.include_router(conversations.router)
    app.include_router(files.router)
    app.include_router(playground.router)
    app.mount(
        "/playground-static",
        StaticFiles(directory=WEB_STATIC_DIR),
        name="playground-static",
    )

    # Global exception handler — wraps unhandled errors in OpenAI error shape
    @app.exception_handler(Exception)
    async def _global_exception_handler(request: Request, exc: Exception):
        return await http_exception_handler(request, exc)

    @app.exception_handler(TBoxUpstreamError)
    async def _tbox_error_handler(request: Request, exc: TBoxUpstreamError):
        return openai_error(str(exc), error_type="upstream_error", status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(request: Request, exc: RequestValidationError):
        first_error = exc.errors()[0] if exc.errors() else {}
        msg = first_error.get("msg", "Invalid request body")
        loc = ".".join(str(x) for x in first_error.get("loc", []))
        detail = f"{loc}: {msg}" if loc else msg
        return openai_error(
            detail,
            error_type="invalid_request_error",
            code="invalid_request_error",
            status_code=400,
        )

    @app.get("/", tags=["root"])
    async def root():
        """Root endpoint - returns basic API information and available endpoints."""
        return {
            "message": "Welcome to tbox-openai-adapter",
            "version": "0.1.0",
            "documentation": {
                "swagger_ui": "/docs",
                "redoc": "/redoc",
                "playground": "/playground",
                "health": "/health"
            },
            "api_endpoints": {
                "openai_format": "/openai/v1/...",
                "anthropic_format": "/anthropic/v1/...",
                "legacy_format": "/v1/..."
            },
            "description": "OpenAI-compatible API adapter for TBox workflows"
        }

    @app.get("/health", tags=["health"])
    async def health_check():
        """Liveness probe — returns 200 if the service is running."""
        return {"status": "ok"}

    # ---- Prometheus /metrics endpoint ----
    if settings.metrics_enabled:
        from starlette.responses import Response as StarletteResponse

        @app.get("/metrics", tags=["monitoring"], include_in_schema=False)
        async def prometheus_metrics():
            """
            Expose Prometheus metrics in text exposition format.

            Before generating the output, we update the active_sessions gauge
            with a fresh count from the session store.
            """
            try:
                sessions = await session_store.aall_sessions()
                ACTIVE_SESSIONS.set(len(sessions))
            except Exception:
                pass  # don't fail /metrics if session store is unavailable

            return StarletteResponse(
                content=generate_metrics(),
                media_type=metrics_content_type,
            )

    return app


app = create_app()


# ---------------------------------------------------------------------------
# Direct execution entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    settings = get_settings()

    # Get local IP address for network access
    import socket
    def get_local_ip():
        try:
            # Connect to a public DNS to get local IP
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception:
            return "127.0.0.1"

    local_ip = get_local_ip()

    print(f"🚀 Server starting up...")
    print(f"📡 Local access:     http://127.0.0.1:{settings.port}")
    print(f"🌐 Network access:   http://{local_ip}:{settings.port}")
    print(f"🔧 Debug mode:       {'enabled' if settings.debug else 'disabled'}")
    print(f"📊 Metrics enabled:  {'yes' if settings.metrics_enabled else 'no'}")
    print("")

    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )
