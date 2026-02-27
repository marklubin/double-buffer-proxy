"""Low-level SSE line protocol parser.

Parses raw SSE byte streams into structured events per the SSE specification.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SSEEvent:
    """A single Server-Sent Event."""

    event: str = ""
    data: str = ""
    id: str = ""
    retry: int | None = None

    @property
    def is_empty(self) -> bool:
        return not self.event and not self.data

    def to_bytes(self) -> bytes:
        """Serialize back to SSE wire format."""
        lines: list[str] = []
        if self.event:
            lines.append(f"event: {self.event}")
        if self.data:
            for data_line in self.data.split("\n"):
                lines.append(f"data: {data_line}")
        if self.id:
            lines.append(f"id: {self.id}")
        if self.retry is not None:
            lines.append(f"retry: {self.retry}")
        lines.append("")  # blank line terminates event
        return ("\n".join(lines) + "\n").encode()


@dataclass
class SSEParser:
    """Incremental SSE parser that processes byte chunks into events."""

    _buffer: str = ""
    _current: SSEEvent = field(default_factory=SSEEvent)

    def feed(self, chunk: str) -> list[SSEEvent]:
        """Feed a chunk of text, return any complete events."""
        self._buffer += chunk
        events: list[SSEEvent] = []

        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")

            if not line:
                # Blank line = event dispatch
                if not self._current.is_empty:
                    events.append(self._current)
                self._current = SSEEvent()
                continue

            if line.startswith(":"):
                # Comment, ignore
                continue

            if ":" in line:
                field_name, _, value = line.partition(":")
                if value.startswith(" "):
                    value = value[1:]
            else:
                field_name = line
                value = ""

            if field_name == "event":
                self._current.event = value
            elif field_name == "data":
                if self._current.data:
                    self._current.data += "\n" + value
                else:
                    self._current.data = value
            elif field_name == "id":
                self._current.id = value
            elif field_name == "retry":
                try:
                    self._current.retry = int(value)
                except ValueError:
                    pass

        return events
