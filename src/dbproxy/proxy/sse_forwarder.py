"""SSE forwarding pipeline: parse upstream SSE, intercept key events, forward to client.

Handles streaming responses from the Anthropic API, extracting usage
information and forwarding events to the client in real-time.
"""

from __future__ import annotations

import json
from typing import Any

import structlog
from aiohttp import web

from .sse_parser import SSEEvent, SSEParser

log = structlog.get_logger()


class SSEForwarder:
    """Forwards SSE events from upstream to client, intercepting usage data."""

    def __init__(self, conv_id: str = "") -> None:
        self.conv_id = conv_id
        self.parser = SSEParser()
        self.usage: dict[str, Any] = {}
        self.stop_reason: str | None = None
        self.content_blocks: list[dict[str, Any]] = []
        self._current_block: dict[str, Any] | None = None
        self._accumulated_text: str = ""
        self._has_compaction: bool = False
        self._message_data: dict[str, Any] = {}

    @property
    def has_compaction(self) -> bool:
        """Whether the response contained a compaction block."""
        return self._has_compaction

    def process_event(self, event: SSEEvent) -> SSEEvent:
        """Process a single SSE event, extracting metadata. Returns the event unchanged."""
        if not event.data:
            return event

        try:
            data = json.loads(event.data)
        except (json.JSONDecodeError, ValueError):
            return event

        event_type = data.get("type", "")

        if event_type == "message_start":
            msg = data.get("message", {})
            self._message_data = msg
            self.usage = msg.get("usage", {})

        elif event_type == "content_block_start":
            block = data.get("content_block", {})
            self._current_block = block
            if block.get("type") == "compaction":
                self._has_compaction = True

        elif event_type == "content_block_delta":
            delta = data.get("delta", {})
            delta_type = delta.get("type", "")
            if delta_type == "text_delta":
                self._accumulated_text += delta.get("text", "")
            elif delta_type == "compaction_delta":
                self._has_compaction = True

        elif event_type == "content_block_stop":
            if self._current_block:
                if self._current_block.get("type") == "text":
                    self._current_block["text"] = self._accumulated_text
                self.content_blocks.append(self._current_block)
                self._current_block = None
                self._accumulated_text = ""

        elif event_type == "message_delta":
            delta = data.get("delta", {})
            if "stop_reason" in delta:
                self.stop_reason = delta["stop_reason"]
            usage = data.get("usage", {})
            if usage:
                self.usage.update(usage)

        return event

    async def forward_stream(
        self,
        response_stream: Any,
        client_response: web.StreamResponse,
        max_buffer_bytes: int = 50_000_000,
    ) -> None:
        """Forward an upstream SSE stream to the client, processing events.

        Args:
            response_stream: An async iterator of SSE text chunks from upstream.
            client_response: The aiohttp StreamResponse to write to.
            max_buffer_bytes: Maximum bytes to buffer before raising.
        """
        total_bytes = 0

        async for chunk in response_stream:
            if isinstance(chunk, bytes):
                text = chunk.decode("utf-8", errors="replace")
            else:
                text = chunk

            events = self.parser.feed(text)
            for event in events:
                processed = self.process_event(event)
                event_bytes = processed.to_bytes()
                total_bytes += len(event_bytes)

                if total_bytes > max_buffer_bytes:
                    log.error(
                        "sse_buffer_overflow",
                        conv_id=self.conv_id[:16],
                        total_bytes=total_bytes,
                    )
                    raise RuntimeError(f"SSE buffer overflow: {total_bytes} bytes")

                await client_response.write(event_bytes)

        log.debug(
            "sse_stream_complete",
            conv_id=self.conv_id[:16],
            total_bytes=total_bytes,
            stop_reason=self.stop_reason,
            has_compaction=self._has_compaction,
        )
