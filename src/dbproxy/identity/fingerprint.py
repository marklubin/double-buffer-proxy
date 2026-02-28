"""Conversation fingerprinting for identity tracking.

Uses the session ID from Claude Code's metadata when available (most
reliable), falling back to SHA-256 of system prompt prefix + first
user message content.

The metadata.user_id field from Claude Code contains a stable session
identifier in the form:
    user_{hash}_account_{uuid}_session_{uuid}

The session UUID is unique per Claude Code conversation and stable
across all requests in that conversation.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import structlog

log = structlog.get_logger()

# How many characters of the system prompt to include in the fallback
# fingerprint.  Claude Code's stable base instructions are several KB;
# 1000 chars captures the identity without the dynamic tail.
SYSTEM_PREFIX_LENGTH = 1000

# Extract session UUID from metadata.user_id
_SESSION_RE = re.compile(r"_session_([0-9a-f-]+)$")


def _extract_session_id(body: dict[str, Any]) -> str | None:
    """Extract the session UUID from Claude Code's metadata.user_id."""
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return None
    user_id = metadata.get("user_id")
    if not isinstance(user_id, str):
        return None
    m = _SESSION_RE.search(user_id)
    return m.group(1) if m else None


def _fallback_fingerprint(body: dict[str, Any]) -> str:
    """Compute a fingerprint from system prompt prefix + first user message.

    Used when metadata.user_id is not available.
    """
    parts: list[str] = []

    # System prompt (prefix only — tail may change between requests)
    system = body.get("system")
    if system is not None:
        if isinstance(system, str):
            parts.append(system[:SYSTEM_PREFIX_LENGTH])
        elif isinstance(system, list):
            serialized = json.dumps(system, sort_keys=True)
            parts.append(serialized[:SYSTEM_PREFIX_LENGTH])

    # First user message
    messages = body.get("messages", [])
    for msg in messages:
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(content)
            elif isinstance(content, list):
                parts.append(json.dumps(content, sort_keys=True))
            break

    combined = "\n---\n".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


def compute_fingerprint(body: dict[str, Any]) -> str:
    """Compute a conversation fingerprint from a /v1/messages request.

    Prefers the session ID from metadata.user_id (stable, unique per
    conversation).  Falls back to hashing system prompt + first user
    message if metadata is not available.
    """
    metadata = body.get("metadata", {})
    user_id = metadata.get("user_id", "") if isinstance(metadata, dict) else ""

    session_id = _extract_session_id(body)
    if session_id:
        log.debug(
            "fingerprint_session",
            session_id=session_id[:16],
            user_id=user_id[:80] if user_id else None,
        )
        return session_id

    fp = _fallback_fingerprint(body)
    log.debug("fingerprint_fallback", fingerprint=fp[:16])
    return fp
