"""Rewrite intercepted /v1/messages requests.

Strips the compact_20260112 context management edit to suppress native
compaction while preserving other edits (e.g. clear_thinking_20251015).
"""

from __future__ import annotations

import copy
from typing import Any

import structlog

log = structlog.get_logger()

COMPACT_EDIT_TYPE = "compact_20260112"


def strip_compact_edit(body: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of the request body with compact_20260112 edit removed.

    Preserves other context_management edits. If no edits remain,
    removes the context_management key entirely.
    """
    ctx_mgmt = body.get("context_management")
    if not ctx_mgmt:
        return body

    edits = ctx_mgmt.get("edits")
    if not edits:
        return body

    filtered = [e for e in edits if e.get("type") != COMPACT_EDIT_TYPE]

    if len(filtered) == len(edits):
        # No compact edit found, return as-is
        return body

    result = copy.deepcopy(body)
    if filtered:
        result["context_management"]["edits"] = filtered
    else:
        del result["context_management"]

    log.debug("compact_edit_stripped", remaining_edits=len(filtered))
    return result


def has_compact_edit(body: dict[str, Any]) -> bool:
    """Check if the request contains a compact_20260112 edit."""
    ctx_mgmt = body.get("context_management")
    if not ctx_mgmt:
        return False
    edits = ctx_mgmt.get("edits", [])
    return any(e.get("type") == COMPACT_EDIT_TYPE for e in edits)


def has_compaction_block(body: dict[str, Any]) -> bool:
    """Check if the request messages contain a compaction content block.

    This indicates the client already has a compaction — we should
    reset conversation state to IDLE.
    """
    messages = body.get("messages", [])
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "compaction":
                    return True
    return False


def strip_compaction_blocks(body: dict[str, Any]) -> dict[str, Any]:
    """Strip compaction blocks from messages before forwarding to API.

    After our synthetic swap, Claude Code sends the compaction block back
    in the next request. The API may reject it (empty content, missing beta).
    Convert compaction blocks to plain text blocks so the API accepts them.
    """
    messages = body.get("messages", [])
    has_any = False
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "compaction":
                    has_any = True
                    break
        if has_any:
            break

    if not has_any:
        return body

    result = copy.deepcopy(body)
    for msg in result["messages"]:
        content = msg.get("content")
        if isinstance(content, list):
            for i, block in enumerate(content):
                if isinstance(block, dict) and block.get("type") == "compaction":
                    # Convert to text block — preserves the summary for the model
                    compaction_text = block.get("content", "")
                    content[i] = {
                        "type": "text",
                        "text": compaction_text or "[conversation summary]",
                    }

    log.info("compaction_blocks_stripped")
    return result


def extract_request_metadata(body: dict[str, Any]) -> dict[str, Any]:
    """Extract key metadata from a /v1/messages request body."""
    return {
        "model": body.get("model", ""),
        "stream": body.get("stream", False),
        "system": body.get("system"),
        "tools": body.get("tools"),
        "max_tokens": body.get("max_tokens"),
        "messages": body.get("messages", []),
    }
