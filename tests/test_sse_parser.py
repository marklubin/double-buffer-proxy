"""Tests for SSE parser."""

from dbproxy.proxy.sse_parser import SSEEvent, SSEParser


class TestSSEEvent:
    def test_to_bytes_basic(self):
        event = SSEEvent(event="message_start", data='{"type":"message_start"}')
        result = event.to_bytes()
        assert b"event: message_start\n" in result
        assert b'data: {"type":"message_start"}\n' in result

    def test_to_bytes_multiline_data(self):
        event = SSEEvent(data="line1\nline2")
        result = event.to_bytes()
        assert b"data: line1\n" in result
        assert b"data: line2\n" in result

    def test_to_bytes_with_id(self):
        event = SSEEvent(event="test", data="hello", id="42")
        result = event.to_bytes()
        assert b"id: 42\n" in result

    def test_is_empty(self):
        assert SSEEvent().is_empty
        assert not SSEEvent(event="test").is_empty
        assert not SSEEvent(data="test").is_empty


class TestSSEParser:
    def test_single_event(self):
        parser = SSEParser()
        events = parser.feed("event: test\ndata: hello\n\n")
        assert len(events) == 1
        assert events[0].event == "test"
        assert events[0].data == "hello"

    def test_multiple_events(self):
        parser = SSEParser()
        events = parser.feed("event: a\ndata: 1\n\nevent: b\ndata: 2\n\n")
        assert len(events) == 2
        assert events[0].event == "a"
        assert events[1].event == "b"

    def test_incremental_feed(self):
        parser = SSEParser()
        assert parser.feed("event: te") == []
        assert parser.feed("st\nda") == []
        events = parser.feed("ta: hello\n\n")
        assert len(events) == 1
        assert events[0].event == "test"
        assert events[0].data == "hello"

    def test_multiline_data(self):
        parser = SSEParser()
        events = parser.feed("data: line1\ndata: line2\n\n")
        assert len(events) == 1
        assert events[0].data == "line1\nline2"

    def test_comment_ignored(self):
        parser = SSEParser()
        events = parser.feed(": comment\nevent: test\ndata: hi\n\n")
        assert len(events) == 1
        assert events[0].event == "test"

    def test_field_without_value(self):
        parser = SSEParser()
        events = parser.feed("event\ndata: hi\n\n")
        assert len(events) == 1
        assert events[0].event == ""

    def test_retry_field(self):
        parser = SSEParser()
        events = parser.feed("retry: 3000\ndata: hi\n\n")
        assert len(events) == 1
        assert events[0].retry == 3000

    def test_leading_space_stripped(self):
        parser = SSEParser()
        events = parser.feed("data: hello world\n\n")
        assert events[0].data == "hello world"

    def test_roundtrip(self):
        original = SSEEvent(event="test", data="hello\nworld")
        parser = SSEParser()
        events = parser.feed(original.to_bytes().decode())
        assert len(events) == 1
        assert events[0].event == original.event
        assert events[0].data == original.data
