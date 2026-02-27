"""Tests for request rewriter."""

from dbproxy.proxy.request_rewriter import (
    extract_request_metadata,
    has_compact_edit,
    has_compaction_block,
    strip_compact_edit,
)


def _make_body(**kwargs):
    base = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 8192,
        "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    }
    base.update(kwargs)
    return base


class TestStripCompactEdit:
    def test_no_context_management(self):
        body = _make_body()
        result = strip_compact_edit(body)
        assert result == body

    def test_strips_compact_edit(self):
        body = _make_body(context_management={
            "edits": [
                {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 100000}},
            ]
        })
        result = strip_compact_edit(body)
        assert "context_management" not in result

    def test_preserves_other_edits(self):
        body = _make_body(context_management={
            "edits": [
                {"type": "compact_20260112", "trigger": {"type": "input_tokens", "value": 100000}},
                {"type": "clear_thinking_20251015"},
            ]
        })
        result = strip_compact_edit(body)
        assert "context_management" in result
        assert len(result["context_management"]["edits"]) == 1
        assert result["context_management"]["edits"][0]["type"] == "clear_thinking_20251015"

    def test_no_mutation_of_original(self):
        body = _make_body(context_management={
            "edits": [
                {"type": "compact_20260112"},
                {"type": "other_edit"},
            ]
        })
        original_len = len(body["context_management"]["edits"])
        strip_compact_edit(body)
        assert len(body["context_management"]["edits"]) == original_len

    def test_no_compact_edit_returns_same(self):
        body = _make_body(context_management={
            "edits": [{"type": "clear_thinking_20251015"}]
        })
        result = strip_compact_edit(body)
        assert result is body  # Same object, not copied


class TestHasCompactEdit:
    def test_true_when_present(self):
        body = _make_body(context_management={
            "edits": [{"type": "compact_20260112"}]
        })
        assert has_compact_edit(body)

    def test_false_when_absent(self):
        body = _make_body()
        assert not has_compact_edit(body)

    def test_false_with_other_edits(self):
        body = _make_body(context_management={
            "edits": [{"type": "clear_thinking_20251015"}]
        })
        assert not has_compact_edit(body)


class TestHasCompactionBlock:
    def test_true_when_compaction_in_messages(self):
        body = _make_body(messages=[
            {"role": "assistant", "content": [{"type": "compaction", "content": "summary"}]},
            {"role": "user", "content": "continue"},
        ])
        assert has_compaction_block(body)

    def test_false_when_no_compaction(self):
        body = _make_body()
        assert not has_compaction_block(body)

    def test_false_with_text_blocks(self):
        body = _make_body(messages=[
            {"role": "assistant", "content": [{"type": "text", "text": "hello"}]},
        ])
        assert not has_compaction_block(body)


class TestExtractRequestMetadata:
    def test_extracts_fields(self):
        body = _make_body(system="You are helpful")
        meta = extract_request_metadata(body)
        assert meta["model"] == "claude-sonnet-4-6"
        assert meta["stream"] is True
        assert meta["system"] == "You are helpful"
        assert len(meta["messages"]) == 1
