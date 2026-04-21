"""
Session store mapping OpenAI `user` -> TBox `conversationId`.

Supports two backends:
  - **Memory** (default): OrderedDict with TTL + max-size eviction.
    Suitable for single-instance deployments.
  - **Redis** (optional): Set REDIS_URL in .env.
    Suitable for multi-instance / production deployments.

The module auto-selects the backend at first use based on config.
All public functions have the same signatures regardless of backend.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------

_backend: Optional["_SessionBackend"] = None


class _SessionBackend:
    """Base class for session store backends."""

    async def get(self, user: str) -> Optional[str]:
        raise NotImplementedError

    async def set(self, user: str, conversation_id: str) -> None:
        raise NotImplementedError

    async def delete(self, user: str) -> None:
        raise NotImplementedError

    async def all(self) -> dict[str, str]:
        raise NotImplementedError

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# In-memory backend with TTL + LRU eviction
# ---------------------------------------------------------------------------


class _MemoryBackend(_SessionBackend):
    """
    In-memory session store with TTL expiration and max-size LRU eviction.

    Each entry stores (conversationId, timestamp). On access, expired entries
    are pruned. When the store exceeds max_size, the oldest entries are evicted.
    """

    def __init__(self, ttl: int = 3600, max_size: int = 10000) -> None:
        self._ttl = ttl
        self._max_size = max_size
        self._last_sweep_at = 0.0
        self._sweep_interval_seconds = 30.0
        # user -> (conversationId, created_at)
        self._store: OrderedDict[str, tuple[str, float]] = OrderedDict()

    def _is_expired(self, entry: tuple[str, float]) -> bool:
        return (time.time() - entry[1]) > self._ttl

    def _evict_expired(self) -> None:
        """Remove all expired entries from the front of the OrderedDict."""
        now = time.time()
        # Entries are ordered by insertion/access time — oldest first
        keys_to_remove = []
        for key, (_, created_at) in self._store.items():
            if (now - created_at) > self._ttl:
                keys_to_remove.append(key)
            else:
                break  # remaining entries are newer
        for key in keys_to_remove:
            del self._store[key]
        if keys_to_remove:
            logger.debug("session_store: evicted %d expired entries", len(keys_to_remove))

    def _enforce_max_size(self) -> None:
        """Evict oldest entries if store exceeds max_size."""
        while len(self._store) > self._max_size:
            evicted_key, _ = self._store.popitem(last=False)
            logger.debug("session_store: LRU evicted user=%s", evicted_key)

    def _maybe_sweep(self) -> None:
        now = time.time()
        if now - self._last_sweep_at >= self._sweep_interval_seconds:
            self._evict_expired()
            self._last_sweep_at = now

    async def get(self, user: str) -> Optional[str]:
        self._maybe_sweep()
        entry = self._store.get(user)
        if entry is None:
            return None
        if self._is_expired(entry):
            del self._store[user]
            logger.debug("session_store: expired entry for user=%s", user)
            return None
        # Move to end (most recently accessed)
        self._store.move_to_end(user)
        return entry[0]

    async def set(self, user: str, conversation_id: str) -> None:
        self._maybe_sweep()
        self._store[user] = (conversation_id, time.time())
        self._store.move_to_end(user)
        self._enforce_max_size()
        logger.debug("session_store: set user=%s conversationId=%s", user, conversation_id)

    async def delete(self, user: str) -> None:
        self._store.pop(user, None)
        logger.debug("session_store: cleared user=%s", user)

    async def all(self) -> dict[str, str]:
        self._maybe_sweep()
        return {k: v[0] for k, v in self._store.items()}


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------


class _RedisBackend(_SessionBackend):
    """
    Redis-backed session store.

    Uses Redis string keys with TTL for automatic expiration.
    Key format: tbox:session:{user}
    """

    def __init__(self, redis_url: str, ttl: int = 3600) -> None:
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl
        self._prefix = "tbox:session:"

    def _key(self, user: str) -> str:
        return f"{self._prefix}{user}"

    async def get(self, user: str) -> Optional[str]:
        return await self._redis.get(self._key(user))

    async def set(self, user: str, conversation_id: str) -> None:
        await self._redis.setex(self._key(user), self._ttl, conversation_id)
        logger.debug("session_store[redis]: set user=%s conversationId=%s", user, conversation_id)

    async def delete(self, user: str) -> None:
        await self._redis.delete(self._key(user))
        logger.debug("session_store[redis]: cleared user=%s", user)

    async def all(self) -> dict[str, str]:
        keys = []
        async for key in self._redis.scan_iter(f"{self._prefix}*"):
            keys.append(key)
        result = {}
        for key in keys:
            val = await self._redis.get(key)
            if val:
                user = key[len(self._prefix):]
                result[user] = val
        return result

    async def close(self) -> None:
        await self._redis.aclose()
        logger.info("session_store[redis]: connection closed")


# ---------------------------------------------------------------------------
# Backend initialisation
# ---------------------------------------------------------------------------


def _get_backend() -> _SessionBackend:
    """Lazily initialise and return the session backend."""
    global _backend
    if _backend is not None:
        return _backend

    from app.core.config import get_settings

    settings = get_settings()

    if settings.redis_url:
        try:
            _backend = _RedisBackend(settings.redis_url, ttl=settings.session_ttl)
            logger.info(
                "session_store: using Redis backend (ttl=%ds)", settings.session_ttl
            )
        except Exception as exc:
            logger.warning(
                "session_store: failed to connect to Redis (%s), falling back to memory",
                exc,
            )
            _backend = _MemoryBackend(
                ttl=settings.session_ttl, max_size=settings.session_max_size
            )
    else:
        _backend = _MemoryBackend(
            ttl=settings.session_ttl, max_size=settings.session_max_size
        )
        logger.info(
            "session_store: using in-memory backend (ttl=%ds, max_size=%d)",
            settings.session_ttl,
            settings.session_max_size,
        )

    return _backend


# ---------------------------------------------------------------------------
# Public API — kept synchronous-looking for callers, but backend may be async
# ---------------------------------------------------------------------------

# Note: Because the adapters call these from async context, we provide both
# async and sync wrappers. The sync versions are kept for backward compat
# with existing tests.


async def aget_conversation_id(user: str) -> Optional[str]:
    """Return the TBox conversationId for *user*, or None if not yet mapped."""
    return await _get_backend().get(user)


async def aset_conversation_id(user: str, conversation_id: str) -> None:
    """Persist the TBox conversationId for *user*."""
    await _get_backend().set(user, conversation_id)


async def aclear_conversation(user: str) -> None:
    """Remove the session mapping for *user* (force a new TBox conversation)."""
    await _get_backend().delete(user)


async def aall_sessions() -> dict[str, str]:
    """Return a snapshot of all sessions."""
    return await _get_backend().all()


async def aclose() -> None:
    """Close the backend connection (call during app shutdown)."""
    backend = _get_backend()
    await backend.close()


# ---------------------------------------------------------------------------
# Sync wrappers — for backward compatibility with existing sync callers/tests
# ---------------------------------------------------------------------------


def get_conversation_id(user: str) -> Optional[str]:
    """Sync wrapper: return conversationId (memory backend only)."""
    backend = _get_backend()
    if isinstance(backend, _MemoryBackend):
        entry = backend._store.get(user)
        if entry is None:
            return None
        if backend._is_expired(entry):
            del backend._store[user]
            return None
        backend._store.move_to_end(user)
        return entry[0]
    raise RuntimeError("Sync access not supported with Redis backend; use aget_conversation_id()")


def set_conversation_id(user: str, conversation_id: str) -> None:
    """Sync wrapper: persist conversationId (memory backend only)."""
    backend = _get_backend()
    if isinstance(backend, _MemoryBackend):
        import asyncio
        # Direct sync call for memory backend
        backend._store[user] = (conversation_id, time.time())
        backend._store.move_to_end(user)
        backend._enforce_max_size()
        logger.debug("session_store: set user=%s conversationId=%s", user, conversation_id)
        return
    raise RuntimeError("Sync access not supported with Redis backend; use aset_conversation_id()")


def clear_conversation(user: str) -> None:
    """Sync wrapper: clear session (memory backend only)."""
    backend = _get_backend()
    if isinstance(backend, _MemoryBackend):
        backend._store.pop(user, None)
        logger.debug("session_store: cleared user=%s", user)
        return
    raise RuntimeError("Sync access not supported with Redis backend; use aclear_conversation()")


def all_sessions() -> dict[str, str]:
    """Sync wrapper: return all sessions (memory backend only)."""
    backend = _get_backend()
    if isinstance(backend, _MemoryBackend):
        backend._evict_expired()
        return {k: v[0] for k, v in backend._store.items()}
    raise RuntimeError("Sync access not supported with Redis backend; use aall_sessions()")


def reset_backend() -> None:
    """Reset the backend instance (useful for testing)."""
    global _backend
    _backend = None
