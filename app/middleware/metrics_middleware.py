"""
Prometheus metrics collection middleware.

Records per-request HTTP metrics:
  - http_requests_total          (Counter)
  - http_request_duration_seconds (Histogram)
  - http_requests_in_progress    (Gauge)

Labels are normalised through ``metrics.classify_path`` to prevent label
cardinality explosion from path parameters (e.g. file IDs, conversation IDs).

The middleware is intentionally positioned *after* authentication in the
middleware stack so that 401 responses are also counted — but it skips the
``/metrics`` endpoint itself to avoid recursion/noise.
"""

from __future__ import annotations

import time
import logging

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from app.utils.metrics import (
    HTTP_REQUESTS_TOTAL,
    HTTP_REQUEST_DURATION_SECONDS,
    HTTP_REQUESTS_IN_PROGRESS,
    classify_path,
)

logger = logging.getLogger(__name__)


class MetricsMiddleware(BaseHTTPMiddleware):
    """
    ASGI middleware that records HTTP request metrics.

    Placement in stack:
        CORSMiddleware → MetricsMiddleware → ApiKeyAuthMiddleware → routes

    This means:
      - Preflight OPTIONS requests are counted (after CORS)
      - Auth failures (401) are counted
      - The /metrics endpoint itself is NOT measured to avoid noise
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        path = request.url.path
        method = request.method

        # Skip the /metrics endpoint itself — no need to track it
        if path == "/metrics":
            return await call_next(request)

        path_template, api_format = classify_path(path)

        # Track in-progress requests
        in_progress = HTTP_REQUESTS_IN_PROGRESS.labels(
            method=method,
            path_template=path_template,
        )
        in_progress.inc()

        start_time = time.perf_counter()
        status_code = 500  # default in case call_next raises

        try:
            response: Response = await call_next(request)
            status_code = response.status_code
            return response
        except Exception:
            # Re-raise — the global exception handler will produce the response
            raise
        finally:
            duration = time.perf_counter() - start_time
            in_progress.dec()

            status_str = str(status_code)

            HTTP_REQUESTS_TOTAL.labels(
                method=method,
                path_template=path_template,
                status_code=status_str,
                api_format=api_format,
            ).inc()

            HTTP_REQUEST_DURATION_SECONDS.labels(
                method=method,
                path_template=path_template,
                status_code=status_str,
                api_format=api_format,
            ).observe(duration)
