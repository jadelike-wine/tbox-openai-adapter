"""
Application configuration loaded from environment variables.

All required variables raise a clear error if missing.
Optional variables have sensible defaults.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ---- TBox upstream ----
    tbox_base_url: str = "https://api.tbox.cn"
    tbox_app_id: str = ""  # optional for development/testing
    tbox_token: str = ""   # optional for development/testing
    tbox_timeout: int = 60  # seconds

    # ---- Retry (exponential back-off, non-streaming requests only) ----
    # Maximum number of attempts (1 = no retry)
    tbox_retry_max_attempts: int = 3
    # Initial back-off delay in seconds; doubled on each attempt
    tbox_retry_backoff_base: float = 0.5
    # Maximum back-off delay in seconds
    tbox_retry_backoff_max: float = 10.0

    # ---- Circuit breaker ----
    # Number of consecutive failures before the circuit opens
    tbox_cb_failure_threshold: int = 5
    # Seconds the circuit stays open before moving to half-open
    tbox_cb_recovery_timeout: float = 30.0
    # Number of successful probes in half-open state to close the circuit
    tbox_cb_half_open_probes: int = 2

    # ---- Adapter behaviour ----
    adapter_model_id: str = "tbox-codex"
    adapter_default_user: str = "default-user"

    # ---- Authentication ----
    # Comma-separated list of valid API keys.
    # If AUTH_REQUIRED=true and API_KEYS is empty, service startup fails.
    api_keys: str = ""
    auth_required: bool = False

    # ---- Session store ----
    # TTL in seconds for session entries (default: 1 hour)
    session_ttl: int = 3600
    # Maximum number of sessions to keep in memory (LRU eviction)
    session_max_size: int = 10000
    # Redis URL for distributed session store (optional; leave empty for in-memory)
    # Example: redis://localhost:6379/0
    redis_url: str = ""

    # ---- Logging ----
    # Set to "json" for structured JSON logs (production), "text" for plain text
    log_format: str = "text"

    # ---- Metrics ----
    # Set to true to expose Prometheus /metrics endpoint and enable metrics middleware
    metrics_enabled: bool = True

    # ---- Server ----
    host: str = "0.0.0.0"
    port: int = 2233
    debug: bool = False
    # Seconds to wait for active SSE streams to finish before forcing shutdown.
    # Set to 0 to disable graceful drain and close immediately.
    shutdown_timeout: float = 30.0

    @property
    def api_keys_set(self) -> set[str]:
        """Parse comma-separated API keys into a set for O(1) lookup."""
        if not self.api_keys.strip():
            return set()
        return {k.strip() for k in self.api_keys.split(",") if k.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance.

    The lru_cache ensures the .env file is only parsed once per process,
    and the same object is shared across all DI injection sites.
    """
    try:
        return Settings()
    except Exception as exc:
        # Re-raise with a human-friendly message so it's obvious which
        # variable is missing when the service fails to start.
        raise RuntimeError(
            f"Failed to load configuration — check your .env file.\n"
            f"Detail: {exc}"
        ) from exc
