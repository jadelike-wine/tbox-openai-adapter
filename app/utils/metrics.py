"""
Prometheus metrics definitions for tbox-openai-adapter.

All metrics are defined here as module-level singletons so they can be
imported anywhere without circular dependencies.

Metric families
---------------
**HTTP request metrics (populated by metrics middleware):**
  - http_requests_total          — Counter by method, path, status, api_format
  - http_request_duration_seconds — Histogram by method, path, status, api_format
  - http_requests_in_progress    — Gauge by method, path

**Business-level metrics (populated by route handlers / adapters):**
  - chat_requests_total          — Counter by model, user, stream, api_format
  - chat_request_errors_total    — Counter by model, user, error_type, api_format

**TBox upstream metrics (populated by tbox_client instrumentation):**
  - tbox_upstream_requests_total — Counter by operation, status
  - tbox_upstream_duration_seconds — Histogram by operation
  - tbox_upstream_errors_total   — Counter by operation, error_type

**Session metrics (populated via callback from session_store):**
  - active_sessions              — Gauge (current number of live sessions)

**SSE stream metrics (populated by tbox_client stream tracking):**
  - active_sse_streams           — Gauge (current number of open SSE streams)

**Circuit breaker metrics:**
  - circuit_breaker_state        — Gauge (0=closed, 1=open, 2=half_open)
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

# ---------------------------------------------------------------------------
# Shared registry — use the default global registry so that
# prometheus_client.generate_latest() picks everything up automatically.
# ---------------------------------------------------------------------------

# We use the default registry (no custom CollectorRegistry) so that the
# standard process/platform collectors are also exported.

# ---------------------------------------------------------------------------
# HTTP request metrics (populated by MetricsMiddleware)
# ---------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total number of HTTP requests received",
    ["method", "path_template", "status_code", "api_format"],
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path_template", "status_code", "api_format"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

HTTP_REQUESTS_IN_PROGRESS = Gauge(
    "http_requests_in_progress",
    "Number of HTTP requests currently being processed",
    ["method", "path_template"],
)

# ---------------------------------------------------------------------------
# Business-level chat metrics (populated by route handlers)
# ---------------------------------------------------------------------------

CHAT_REQUESTS_TOTAL = Counter(
    "chat_requests_total",
    "Total chat completion requests by model, user, streaming mode, and API format",
    ["model", "user", "stream", "api_format"],
)

CHAT_REQUEST_ERRORS_TOTAL = Counter(
    "chat_request_errors_total",
    "Total chat completion errors by model, user, error type, and API format",
    ["model", "user", "error_type", "api_format"],
)

# ---------------------------------------------------------------------------
# TBox upstream metrics (populated by tbox_client instrumentation)
# ---------------------------------------------------------------------------

TBOX_UPSTREAM_REQUESTS_TOTAL = Counter(
    "tbox_upstream_requests_total",
    "Total requests sent to TBox upstream",
    ["operation", "status"],
)

TBOX_UPSTREAM_DURATION_SECONDS = Histogram(
    "tbox_upstream_duration_seconds",
    "TBox upstream request latency in seconds",
    ["operation"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0),
)

TBOX_UPSTREAM_ERRORS_TOTAL = Counter(
    "tbox_upstream_errors_total",
    "Total errors from TBox upstream by operation and error type",
    ["operation", "error_type"],
)

# ---------------------------------------------------------------------------
# Session metrics
# ---------------------------------------------------------------------------

ACTIVE_SESSIONS = Gauge(
    "active_sessions",
    "Current number of active user sessions in the session store",
)

# ---------------------------------------------------------------------------
# SSE stream metrics
# ---------------------------------------------------------------------------

ACTIVE_SSE_STREAMS = Gauge(
    "active_sse_streams",
    "Current number of open SSE streaming connections to TBox",
)

# ---------------------------------------------------------------------------
# Circuit breaker metrics
# ---------------------------------------------------------------------------

CIRCUIT_BREAKER_STATE = Gauge(
    "circuit_breaker_state",
    "Circuit breaker state: 0=closed, 1=open, 2=half_open",
    ["name"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Re-export for convenience
metrics_content_type = CONTENT_TYPE_LATEST
generate_metrics = generate_latest


def classify_path(path: str) -> tuple[str, str]:
    """
    Normalise a request path into (path_template, api_format).

    Returns a stable template string (e.g. "/v1/chat/completions") and
    the API format ("openai", "anthropic", or "internal").

    This prevents label cardinality explosion from path parameters.
    """
    # Strip trailing slash for consistency
    p = path.rstrip("/")

    # Determine api_format based on prefix
    api_format = "openai"
    if p.startswith("/anthropic"):
        api_format = "anthropic"
        p = p[len("/anthropic"):]
    elif p.startswith("/openai"):
        api_format = "openai"
        p = p[len("/openai"):]

    # Normalise known routes to templates to collapse path params
    if p.startswith("/v1/conversations/") and p.endswith("/messages"):
        return "/v1/conversations/{id}/messages", api_format
    if p.startswith("/v1/files/") and p != "/v1/files":
        return "/v1/files/{file_id}", api_format
    if p == "/health":
        return "/health", "internal"
    if p == "/metrics":
        return "/metrics", "internal"

    # Known static paths — return as-is
    known = {
        "/v1/chat/completions",
        "/v1/messages",
        "/v1/models",
        "/v1/conversations",
        "/v1/files",
    }
    if p in known:
        return p, api_format

    # Fallback — avoid cardinality explosion
    return "/other", api_format
