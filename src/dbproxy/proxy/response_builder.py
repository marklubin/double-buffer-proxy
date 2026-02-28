"""Build synthetic compaction responses for swap execution.

Constructs both JSON and SSE-format responses containing the stored
compaction block, mimicking the native Anthropic API compaction response.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .sse_parser import SSEEvent


def generate_message_id() -> str:
    """Generate a msg_ prefixed ID."""
    import hashlib
    return "msg_dbproxy_" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:24]


def build_compaction_json(
    compaction_content: str,
    model: str,
) -> dict[str, Any]:
    """Build a non-streaming summary response.

    Returns a regular text response (not a compaction block) because
    Claude Code expects compaction responses as plain assistant messages.
    """
    return {
        "id": generate_message_id(),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": compaction_content}],
        "model": model,
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": len(compaction_content) // 4},
    }


def build_compaction_sse_events(
    compaction_content: str,
    model: str,
) -> list[SSEEvent]:
    """Build SSE events for a streaming summary response.

    Returns a regular text streaming response (not compaction blocks)
    because Claude Code expects compaction responses as plain assistant
    messages.

    Sequence:
    1. message_start
    2. content_block_start (text)
    3. content_block_delta (text_delta with complete content)
    4. content_block_stop
    5. message_delta (stop_reason=end_turn)
    6. message_stop
    """
    msg_id = generate_message_id()
    output_tokens = len(compaction_content) // 4

    events = [
        SSEEvent(
            event="message_start",
            data=json.dumps({
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 1},
                },
            }),
        ),
        SSEEvent(
            event="content_block_start",
            data=json.dumps({
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            }),
        ),
        SSEEvent(
            event="content_block_delta",
            data=json.dumps({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": compaction_content},
            }),
        ),
        SSEEvent(
            event="content_block_stop",
            data=json.dumps({
                "type": "content_block_stop",
                "index": 0,
            }),
        ),
        SSEEvent(
            event="message_delta",
            data=json.dumps({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": output_tokens},
            }),
        ),
        SSEEvent(
            event="message_stop",
            data=json.dumps({"type": "message_stop"}),
        ),
    ]

    return events
