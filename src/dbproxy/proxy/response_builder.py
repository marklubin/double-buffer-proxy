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
    """Build a non-streaming compaction response."""
    return {
        "id": generate_message_id(),
        "type": "message",
        "role": "assistant",
        "content": [{"type": "compaction", "content": compaction_content}],
        "model": model,
        "stop_reason": "compaction",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def build_compaction_sse_events(
    compaction_content: str,
    model: str,
) -> list[SSEEvent]:
    """Build SSE events for a streaming compaction response.

    Returns the sequence:
    1. message_start
    2. content_block_start (compaction)
    3. content_block_delta (compaction_delta with complete content)
    4. content_block_stop
    5. message_delta (stop_reason=compaction)
    6. message_stop
    """
    msg_id = generate_message_id()

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
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            }),
        ),
        SSEEvent(
            event="content_block_start",
            data=json.dumps({
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "compaction", "content": ""},
            }),
        ),
        SSEEvent(
            event="content_block_delta",
            data=json.dumps({
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "compaction_delta", "content": compaction_content},
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
                "delta": {"stop_reason": "compaction", "stop_sequence": None},
                "usage": {"output_tokens": 0},
            }),
        ),
        SSEEvent(
            event="message_stop",
            data=json.dumps({"type": "message_stop"}),
        ),
    ]

    return events
