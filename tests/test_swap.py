"""Tests for swap module."""

import json

from dbproxy.buffer.swap import (
    _serialize_message,
    build_swap_response,
    format_compaction_with_wal,
    serialize_swap_response_bytes,
)


class TestSerializeMessage:
    def test_string_content(self):
        msg = {"role": "user", "content": "hello"}
        assert _serialize_message(msg) == "[user]\nhello"

    def test_text_block(self):
        msg = {"role": "assistant", "content": [{"type": "text", "text": "hi there"}]}
        assert _serialize_message(msg) == "[assistant]\nhi there"

    def test_tool_use_block(self):
        msg = {"role": "assistant", "content": [
            {"type": "tool_use", "name": "read_file", "input": {"path": "/foo.py"}},
        ]}
        result = _serialize_message(msg)
        assert "[tool_use: read_file(" in result

    def test_tool_result_block_string(self):
        msg = {"role": "user", "content": [
            {"type": "tool_result", "content": "file contents here"},
        ]}
        result = _serialize_message(msg)
        assert "[tool_result: file contents here]" in result

    def test_tool_result_block_list(self):
        msg = {"role": "user", "content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "ok"}]},
        ]}
        result = _serialize_message(msg)
        assert "[tool_result: ok]" in result

    def test_compaction_block(self):
        msg = {"role": "assistant", "content": [{"type": "compaction", "content": "..."}]}
        assert "[prior compaction summary]" in _serialize_message(msg)

    def test_unknown_block(self):
        msg = {"role": "user", "content": [{"type": "image", "data": "..."}]}
        assert "[image block]" in _serialize_message(msg)

    def test_missing_role(self):
        msg = {"content": "hi"}
        assert _serialize_message(msg).startswith("[unknown]")

    def test_tool_use_input_truncation(self):
        msg = {"role": "assistant", "content": [
            {"type": "tool_use", "name": "write", "input": {"data": "x" * 500}},
        ]}
        result = _serialize_message(msg)
        # JSON serialization of input is truncated to 200 chars
        assert len(result) < 300

    def test_tool_result_truncation(self):
        msg = {"role": "user", "content": [
            {"type": "tool_result", "content": "y" * 1000},
        ]}
        result = _serialize_message(msg)
        assert len(result) < 600


class TestFormatCompactionWithWal:
    def test_no_wal(self):
        assert format_compaction_with_wal("summary", []) == "summary"

    def test_with_wal(self):
        wal = [
            {"role": "user", "content": "what is 2+2?"},
            {"role": "assistant", "content": "4"},
        ]
        result = format_compaction_with_wal("summary", wal)
        assert result.startswith("summary\n\n<recent_activity>")
        assert "[user]\nwhat is 2+2?" in result
        assert "[assistant]\n4" in result
        assert result.endswith("</recent_activity>")

    def test_multiple_messages(self):
        wal = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
        result = format_compaction_with_wal("chk", wal)
        assert result.count("[user]") == 2
        assert result.count("[assistant]") == 1


class TestBuildSwapResponse:
    def test_non_streaming(self):
        result = build_swap_response("summary", "claude-sonnet-4-6", stream=False)
        assert isinstance(result, dict)
        assert result["stop_reason"] == "compaction"
        assert result["content"][0]["content"] == "summary"

    def test_streaming(self):
        result = build_swap_response("summary", "claude-sonnet-4-6", stream=True)
        assert isinstance(result, list)
        assert len(result) == 6

    def test_non_streaming_with_wal(self):
        wal = [{"role": "user", "content": "recent msg"}]
        result = build_swap_response("summary", "claude-sonnet-4-6", stream=False, wal_messages=wal)
        assert isinstance(result, dict)
        content = result["content"][0]["content"]
        assert "summary" in content
        assert "<recent_activity>" in content
        assert "recent msg" in content

    def test_streaming_with_wal(self):
        wal = [{"role": "user", "content": "recent msg"}]
        result = build_swap_response("summary", "claude-sonnet-4-6", stream=True, wal_messages=wal)
        assert isinstance(result, list)
        # Check that the compaction content delta contains the WAL
        sse_bytes = serialize_swap_response_bytes(result, stream=True)
        assert b"recent_activity" in sse_bytes


class TestSerializeSwapResponseBytes:
    def test_json_response(self):
        response = {"type": "message", "content": []}
        result = serialize_swap_response_bytes(response, stream=False)
        assert isinstance(result, bytes)
        parsed = json.loads(result)
        assert parsed["type"] == "message"

    def test_sse_response(self):
        from dbproxy.proxy.response_builder import build_compaction_sse_events
        events = build_compaction_sse_events("test", "claude-sonnet-4-6")
        result = serialize_swap_response_bytes(events, stream=True)
        assert isinstance(result, bytes)
        assert b"event: message_start" in result
        assert b"event: message_stop" in result
