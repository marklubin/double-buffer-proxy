"""Conversation registry: fingerprint → ConversationState mapping."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from dbproxy.buffer.manager import BufferManager

log = structlog.get_logger()


class ConversationRegistry:
    """Thread-safe registry mapping conversation fingerprints to buffer managers."""

    def __init__(self, ttl_seconds: int = 7200) -> None:
        self._conversations: dict[str, BufferManager] = {}
        self._last_seen: dict[str, float] = {}
        self._ttl = ttl_seconds

    def get_or_create(self, fingerprint: str, model: str, context_window: int) -> "BufferManager":
        """Get an existing conversation or create a new one.

        Keyed by (fingerprint, model) so that different models (e.g. haiku
        for system tasks vs opus for main conversation) get separate managers.
        """
        from dbproxy.buffer.manager import BufferManager

        key = f"{fingerprint}:{model}"
        self._last_seen[key] = time.time()

        if key in self._conversations:
            return self._conversations[key]

        mgr = BufferManager(
            conv_id=fingerprint,
            model=model,
            context_window=context_window,
        )
        self._conversations[key] = mgr
        log.info("conversation_registered", conv_id=fingerprint[:16], model=model)
        return mgr

    def get(self, fingerprint: str) -> "BufferManager | None":
        """Get an existing conversation by prefix match, or None."""
        for key, mgr in self._conversations.items():
            if key.startswith(fingerprint):
                self._last_seen[key] = time.time()
                return mgr
        return None

    def remove(self, fingerprint: str) -> None:
        """Remove a conversation from the registry (matches by prefix)."""
        to_remove = [k for k in self._conversations if k.startswith(fingerprint)]
        for k in to_remove:
            self._conversations.pop(k, None)
            self._last_seen.pop(k, None)

    def expire_stale(self) -> list[str]:
        """Remove conversations older than TTL. Returns list of expired keys."""
        now = time.time()
        expired = [
            key for key, ts in self._last_seen.items()
            if now - ts > self._ttl
        ]
        for key in expired:
            self._conversations.pop(key, None)
            self._last_seen.pop(key, None)
            log.info("conversation_expired", key=key[:32])
        return expired

    def all_conversations(self) -> dict[str, "BufferManager"]:
        """Return all active conversations."""
        return dict(self._conversations)

    def __len__(self) -> int:
        return len(self._conversations)
