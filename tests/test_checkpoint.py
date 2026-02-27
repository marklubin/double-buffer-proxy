"""Tests for checkpoint anchor selection."""

from dbproxy.buffer.checkpoint import find_checkpoint_anchor


class TestFindCheckpointAnchor:
    def test_all_resolved(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "file contents"},
            ]},
            {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
        ]
        assert find_checkpoint_anchor(messages) == 4

    def test_unresolved_tool_use(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "bash", "input": {}},
            ]},
            # No tool_result for t1
        ]
        # Anchor should be before the unresolved tool_use (index 3)
        assert find_checkpoint_anchor(messages) == 3

    def test_mixed_resolved_unresolved(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
            ]},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "write", "input": {}},
            ]},
            # t2 is unresolved
        ]
        assert find_checkpoint_anchor(messages) == 3

    def test_no_tool_use(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "bye"},
        ]
        assert find_checkpoint_anchor(messages) == 3

    def test_empty_messages(self):
        assert find_checkpoint_anchor([]) == 0

    def test_string_content(self):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        assert find_checkpoint_anchor(messages) == 2
