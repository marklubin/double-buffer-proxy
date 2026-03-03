"""Tests for checkpoint anchor selection and run_checkpoint."""

import httpx
import pytest

from dbproxy.buffer.checkpoint import find_checkpoint_anchor, run_checkpoint


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


class TestRunCheckpointRetry:
    """Test retry on transient HTTP/2 connection errors."""

    @pytest.fixture
    def _compaction_response(self):
        return {
            "content": [{"type": "compaction", "content": "summary text"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

    @pytest.mark.asyncio
    async def test_retry_on_remote_protocol_error(self, _compaction_response):
        """ConnectionTerminated (GOAWAY) should retry and succeed."""
        call_count = 0

        async def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.RemoteProtocolError(
                    "ConnectionTerminated error_code:0, last_stream_id:19999"
                )
            return httpx.Response(200, json=_compaction_response)

        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as client:
            result = await run_checkpoint(
                http_client=client,
                upstream_url="https://api.test",
                auth_headers={"x-api-key": "test", "anthropic-version": "2023-06-01"},
                model="claude-haiku-4-5-20251001",
                system=None,
                tools=None,
                messages=[{"role": "user", "content": "hello"}],
            )

        assert result == "summary text"
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        """Persistent connection errors should raise after retries exhausted."""
        call_count = 0

        async def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            raise httpx.RemoteProtocolError("connection lost")

        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as client:
            with pytest.raises(httpx.RemoteProtocolError):
                await run_checkpoint(
                    http_client=client,
                    upstream_url="https://api.test",
                    auth_headers={"x-api-key": "test", "anthropic-version": "2023-06-01"},
                    model="claude-haiku-4-5-20251001",
                    system=None,
                    tools=None,
                    messages=[{"role": "user", "content": "hello"}],
                )

        assert call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_http_error(self, _compaction_response):
        """Non-connection errors (e.g. 500) should not retry."""
        call_count = 0

        async def _handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(500, json={"error": "internal"})

        transport = httpx.MockTransport(_handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.test") as client:
            with pytest.raises(httpx.HTTPStatusError):
                await run_checkpoint(
                    http_client=client,
                    upstream_url="https://api.test",
                    auth_headers={"x-api-key": "test", "anthropic-version": "2023-06-01"},
                    model="claude-haiku-4-5-20251001",
                    system=None,
                    tools=None,
                    messages=[{"role": "user", "content": "hello"}],
                )

        assert call_count == 1
