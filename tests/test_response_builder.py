"""Tests for synthetic compaction response builder."""

import json

from dbproxy.proxy.response_builder import (
    build_compaction_json,
    build_compaction_sse_events,
)


class TestBuildCompactionJson:
    def test_structure(self):
        result = build_compaction_json("summary text", "claude-sonnet-4-6")
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["stop_reason"] == "compaction"
        assert result["model"] == "claude-sonnet-4-6"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "compaction"
        assert result["content"][0]["content"] == "summary text"
        assert result["usage"]["input_tokens"] == 0
        assert result["usage"]["output_tokens"] == 0

    def test_id_prefix(self):
        result = build_compaction_json("test", "claude-sonnet-4-6")
        assert result["id"].startswith("msg_dbproxy_")


class TestBuildCompactionSSEEvents:
    def test_event_sequence(self):
        events = build_compaction_sse_events("summary", "claude-sonnet-4-6")
        event_types = [e.event for e in events]
        assert event_types == [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ]

    def test_compaction_content_in_delta(self):
        events = build_compaction_sse_events("my summary", "claude-sonnet-4-6")
        delta_event = events[2]  # content_block_delta
        data = json.loads(delta_event.data)
        assert data["delta"]["type"] == "compaction_delta"
        assert data["delta"]["content"] == "my summary"

    def test_stop_reason_compaction(self):
        events = build_compaction_sse_events("test", "claude-sonnet-4-6")
        msg_delta = events[4]  # message_delta
        data = json.loads(msg_delta.data)
        assert data["delta"]["stop_reason"] == "compaction"

    def test_content_block_type(self):
        events = build_compaction_sse_events("test", "claude-sonnet-4-6")
        block_start = events[1]
        data = json.loads(block_start.data)
        assert data["content_block"]["type"] == "compaction"

    def test_events_serializable(self):
        events = build_compaction_sse_events("test", "claude-sonnet-4-6")
        for event in events:
            raw = event.to_bytes()
            assert isinstance(raw, bytes)
            assert len(raw) > 0
