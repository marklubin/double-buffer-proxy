"""Swap execution: construct compaction block + WAL stitching.

When the swap threshold is reached, this module builds the synthetic
compaction response that Claude Code will treat as native compaction.

The compaction content includes BOTH the checkpoint summary AND the
WAL messages (messages that arrived between checkpoint and swap), so
the compaction block is a complete record of the conversation up to
the swap point.
"""

from __future__ import annotations

import json
from typing import Any

import structlog

from dbproxy.proxy.response_builder import (
    build_compaction_json,
    build_compaction_sse_events,
)
from dbproxy.proxy.sse_parser import SSEEvent

log = structlog.get_logger()


def _serialize_message(msg: dict[str, Any]) -> str:
    """Serialize a single message dict to readable text."""
    role = msg.get("role", "unknown")
    content = msg.get("content", "")

    if isinstance(content, str):
        return f"[{role}]\n{content}"

    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif not isinstance(block, dict):
                parts.append(str(block))
            elif block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                parts.append(
                    f"[tool_use: {block.get('name', '?')}"
                    f"({json.dumps(block.get('input', {}))[:200]})]"
                )
            elif block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = " ".join(
                        b.get("text", "") for b in result_content if isinstance(b, dict)
                    )
                parts.append(f"[tool_result: {str(result_content)[:500]}]")
            elif block.get("type") == "compaction":
                parts.append("[prior compaction summary]")
            else:
                parts.append(f"[{block.get('type', 'unknown')} block]")
        return f"[{role}]\n" + "\n".join(parts)

    return f"[{role}]\n{str(content)}"


def format_compaction_with_wal(
    checkpoint_content: str,
    wal_messages: list[dict[str, Any]],
) -> str:
    """Combine checkpoint summary with serialized WAL messages.

    Returns the full compaction content string that includes both
    the checkpoint summary and recent activity from the WAL.

    A framing note is prepended so that when this summary appears as
    the assistant's first message in the next request, the model knows
    to respond normally to the user's subsequent message rather than
    continuing to summarize.
    """
    parts: list[str] = [
        "<context_summary>",
        "This is a summary of the conversation so far. "
        "All prior context has been incorporated below. "
        "Respond normally to the user's next message.",
        "",
        checkpoint_content,
    ]

    if wal_messages:
        serialized = "\n\n".join(_serialize_message(msg) for msg in wal_messages)
        parts.append("")
        parts.append("<recent_activity>")
        parts.append(serialized)
        parts.append("</recent_activity>")

    parts.append("</context_summary>")

    return "\n".join(parts)


def build_swap_response(
    checkpoint_content: str,
    model: str,
    stream: bool,
    wal_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | list[SSEEvent]:
    """Build the swap response (compaction block) for the client.

    For non-streaming: returns the JSON response dict.
    For streaming: returns a list of SSEEvent objects to send.

    If wal_messages is provided, the compaction content will include
    both the checkpoint summary and the serialized WAL messages.
    """
    compaction_content = format_compaction_with_wal(
        checkpoint_content, wal_messages or []
    )

    log.info(
        "swap_executed",
        model=model,
        stream=stream,
        checkpoint_length=len(checkpoint_content),
        wal_count=len(wal_messages or []),
        compaction_length=len(compaction_content),
    )

    if stream:
        return build_compaction_sse_events(compaction_content, model)
    else:
        return build_compaction_json(compaction_content, model)


def serialize_swap_response_bytes(
    response: dict[str, Any] | list[SSEEvent],
    stream: bool,
) -> bytes:
    """Serialize a swap response to bytes for sending to the client."""
    if stream:
        assert isinstance(response, list)
        parts: list[bytes] = []
        for event in response:
            parts.append(event.to_bytes())
        return b"".join(parts)
    else:
        assert isinstance(response, dict)
        return json.dumps(response).encode()
