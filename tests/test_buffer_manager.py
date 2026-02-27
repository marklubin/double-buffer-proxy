"""Tests for buffer manager."""

import asyncio

import pytest

from dbproxy.buffer.manager import BufferManager
from dbproxy.buffer.state_machine import BufferPhase


class TestBufferManager:
    def test_initial_state(self):
        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000)
        assert mgr.phase == BufferPhase.IDLE
        assert mgr.total_input_tokens == 0
        assert mgr.utilization == 0.0
        assert mgr.checkpoint_content is None

    def test_utilization_calculation(self):
        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000)
        mgr.total_input_tokens = 140_000
        assert mgr.utilization == 0.7

    def test_update_tokens(self):
        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000)
        mgr.update_tokens({
            "input_tokens": 100_000,
            "cache_creation_input_tokens": 20_000,
            "cache_read_input_tokens": 10_000,
        })
        assert mgr.total_input_tokens == 130_000

    def test_should_swap_false_when_idle(self):
        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000)
        assert not mgr.should_swap()

    def test_should_swap_true_when_ready(self):
        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000)
        mgr.phase = BufferPhase.SWAP_READY
        assert mgr.should_swap()

    def test_to_dict(self):
        mgr = BufferManager("abcdef1234567890rest", "claude-sonnet-4-6", 200_000)
        mgr.total_input_tokens = 50_000
        d = mgr.to_dict()
        assert d["conv_id"] == "abcdef1234567890"
        assert d["model"] == "claude-sonnet-4-6"
        assert d["phase"] == "IDLE"
        assert d["utilization"] == 0.25
        assert d["total_input_tokens"] == 50_000

    @pytest.mark.asyncio
    async def test_reset(self):
        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000)
        mgr.phase = BufferPhase.WAL_ACTIVE
        mgr.checkpoint_content = "summary"
        await mgr.reset("test")
        assert mgr.phase == BufferPhase.IDLE
        assert mgr.checkpoint_content is None

    @pytest.mark.asyncio
    async def test_execute_swap(self):
        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000)
        mgr.phase = BufferPhase.SWAP_READY
        mgr.checkpoint_content = "This is a summary of the conversation."
        result = await mgr.execute_swap(stream=False)
        assert isinstance(result, dict)
        assert result["stop_reason"] == "compaction"
        assert result["content"][0]["type"] == "compaction"
        assert mgr.phase == BufferPhase.IDLE

    @pytest.mark.asyncio
    async def test_execute_swap_streaming(self):
        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000)
        mgr.phase = BufferPhase.SWAP_READY
        mgr.checkpoint_content = "Summary"
        result = await mgr.execute_swap(stream=True)
        assert isinstance(result, list)
        assert len(result) == 6  # 6 SSE events
        assert mgr.phase == BufferPhase.IDLE

    @pytest.mark.asyncio
    async def test_execute_swap_wrong_phase(self):
        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000)
        with pytest.raises(RuntimeError, match="Cannot swap"):
            await mgr.execute_swap(stream=False)


class TestThresholdSkip:
    """Test when utilization jumps past both checkpoint AND swap thresholds."""

    @pytest.mark.asyncio
    async def test_idle_to_swap_ready_in_one_jump(self):
        """Utilization jumps from below checkpoint to above swap in one request."""
        from unittest.mock import AsyncMock, patch

        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000,
                            checkpoint_threshold=0.60, swap_threshold=0.80)
        mgr._auth_headers = {"authorization": "Bearer test"}
        mgr._system = "test system"
        mgr._all_messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        # Set tokens to 83% — above both checkpoint (60%) and swap (80%)
        mgr.total_input_tokens = 166_000

        # Mock run_checkpoint to return immediately
        with patch(
            "dbproxy.buffer.manager.run_checkpoint",
            new_callable=AsyncMock,
            return_value="Checkpoint summary",
        ):
            http_client = AsyncMock()
            await mgr.evaluate_thresholds(http_client, "https://api.anthropic.com")

        assert mgr.phase == BufferPhase.SWAP_READY
        assert mgr.checkpoint_content == "Checkpoint summary"

    @pytest.mark.asyncio
    async def test_idle_to_checkpoint_when_below_swap(self):
        """Normal case: crosses checkpoint but not swap threshold."""
        from unittest.mock import AsyncMock, patch

        mgr = BufferManager("test", "claude-sonnet-4-6", 200_000,
                            checkpoint_threshold=0.60, swap_threshold=0.80)
        mgr._auth_headers = {"authorization": "Bearer test"}
        mgr._system = "test system"
        mgr._all_messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        # Set tokens to 65% — above checkpoint (60%) but below swap (80%)
        mgr.total_input_tokens = 130_000

        with patch(
            "dbproxy.buffer.manager.run_checkpoint",
            new_callable=AsyncMock,
            return_value="Checkpoint summary",
        ):
            http_client = AsyncMock()
            await mgr.evaluate_thresholds(http_client, "https://api.anthropic.com")

        # Should NOT be in SWAP_READY — only crossed checkpoint threshold
        assert mgr.phase != BufferPhase.SWAP_READY


class TestSwapWalStitching:
    """Test WAL stitching edge cases during execute_swap."""

    def _make_mgr(self):
        mgr = BufferManager("test-conv", "claude-sonnet-4-6", 200_000)
        mgr.phase = BufferPhase.SWAP_READY
        mgr.checkpoint_content = "Summary of early conversation."
        return mgr

    @pytest.mark.asyncio
    async def test_swap_includes_wal_messages(self):
        """Normal case: messages after checkpoint anchor are stitched in."""
        mgr = self._make_mgr()
        mgr._all_messages = [
            {"role": "user", "content": "old msg 1"},
            {"role": "assistant", "content": "old reply 1"},
            {"role": "user", "content": "new msg after checkpoint"},
            {"role": "assistant", "content": "new reply after checkpoint"},
        ]
        mgr.checkpoint_anchor_index = 2  # checkpoint covered [0:2]

        result = await mgr.execute_swap(stream=False)
        content = result["content"][0]["content"]
        assert "Summary of early conversation." in content
        assert "<recent_activity>" in content
        assert "new msg after checkpoint" in content
        assert "new reply after checkpoint" in content
        # Old messages should NOT be in the WAL section
        assert "old msg 1" not in content

    @pytest.mark.asyncio
    async def test_swap_empty_wal_when_no_messages_after_anchor(self):
        """Checkpoint covered everything — no WAL to stitch."""
        mgr = self._make_mgr()
        mgr._all_messages = [
            {"role": "user", "content": "msg 1"},
            {"role": "assistant", "content": "reply 1"},
        ]
        mgr.checkpoint_anchor_index = 2  # checkpoint covered all messages

        result = await mgr.execute_swap(stream=False)
        content = result["content"][0]["content"]
        assert content == "Summary of early conversation."
        assert "<recent_activity>" not in content

    @pytest.mark.asyncio
    async def test_swap_no_anchor_index(self):
        """checkpoint_anchor_index is None — treat as empty WAL."""
        mgr = self._make_mgr()
        mgr._all_messages = [
            {"role": "user", "content": "msg"},
        ]
        mgr.checkpoint_anchor_index = None

        result = await mgr.execute_swap(stream=False)
        content = result["content"][0]["content"]
        assert content == "Summary of early conversation."
        assert "<recent_activity>" not in content

    @pytest.mark.asyncio
    async def test_swap_empty_all_messages(self):
        """_all_messages is empty (shouldn't happen, but defensive)."""
        mgr = self._make_mgr()
        mgr._all_messages = []
        mgr.checkpoint_anchor_index = 5

        result = await mgr.execute_swap(stream=False)
        content = result["content"][0]["content"]
        assert content == "Summary of early conversation."

    @pytest.mark.asyncio
    async def test_swap_anchor_beyond_messages(self):
        """Anchor index exceeds message count — empty WAL slice."""
        mgr = self._make_mgr()
        mgr._all_messages = [
            {"role": "user", "content": "only msg"},
        ]
        mgr.checkpoint_anchor_index = 10

        result = await mgr.execute_swap(stream=False)
        content = result["content"][0]["content"]
        assert "<recent_activity>" not in content

    @pytest.mark.asyncio
    async def test_swap_wal_with_tool_use_cycle(self):
        """WAL contains tool_use and tool_result — the most common real case."""
        mgr = self._make_mgr()
        mgr.checkpoint_anchor_index = 1
        mgr._all_messages = [
            {"role": "user", "content": "old"},
            # WAL starts here
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me read that file."},
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "/home/user/project/main.py"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": "def main():\n    pass"}]},
            ]},
            {"role": "assistant", "content": [
                {"type": "text", "text": "The file contains a main function."},
            ]},
        ]

        result = await mgr.execute_swap(stream=False)
        content = result["content"][0]["content"]
        assert "[tool_use: Read(" in content
        assert "[tool_result: def main():" in content
        assert "Let me read that file." in content
        assert "The file contains a main function." in content

    @pytest.mark.asyncio
    async def test_swap_wal_with_prior_compaction(self):
        """WAL contains a compaction block from a previous /compact."""
        mgr = self._make_mgr()
        mgr.checkpoint_anchor_index = 0
        mgr._all_messages = [
            {"role": "assistant", "content": [
                {"type": "compaction", "content": "Prior session summary..."},
            ]},
            {"role": "user", "content": "continue working"},
        ]

        result = await mgr.execute_swap(stream=False)
        content = result["content"][0]["content"]
        assert "[prior compaction summary]" in content
        assert "continue working" in content

    @pytest.mark.asyncio
    async def test_swap_wal_truncates_large_tool_results(self):
        """Large tool results in WAL are truncated to 500 chars."""
        mgr = self._make_mgr()
        mgr.checkpoint_anchor_index = 0
        huge_result = "x" * 2000
        mgr._all_messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "content": huge_result},
            ]},
        ]

        result = await mgr.execute_swap(stream=False)
        content = result["content"][0]["content"]
        # The tool result text should be truncated
        assert len(content) < 1000

    @pytest.mark.asyncio
    async def test_swap_wal_streaming(self):
        """WAL stitching works for streaming responses too."""
        mgr = self._make_mgr()
        mgr.checkpoint_anchor_index = 1
        mgr._all_messages = [
            {"role": "user", "content": "old"},
            {"role": "user", "content": "new after checkpoint"},
        ]

        result = await mgr.execute_swap(stream=True)
        assert isinstance(result, list)
        # Serialize and check WAL is present in SSE bytes
        from dbproxy.buffer.swap import serialize_swap_response_bytes
        raw = serialize_swap_response_bytes(result, stream=True)
        assert b"recent_activity" in raw
        assert b"new after checkpoint" in raw

    @pytest.mark.asyncio
    async def test_swap_resets_state_after_wal_stitch(self):
        """After swap with WAL, all state is properly reset."""
        mgr = self._make_mgr()
        mgr.checkpoint_anchor_index = 1
        mgr._all_messages = [
            {"role": "user", "content": "old"},
            {"role": "user", "content": "new"},
        ]

        await mgr.execute_swap(stream=False)
        assert mgr.phase == BufferPhase.IDLE
        assert mgr.checkpoint_content is None
        assert mgr.checkpoint_anchor_index is None
        assert mgr.total_input_tokens == 0
