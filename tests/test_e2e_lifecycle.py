"""End-to-end lifecycle test: IDLE → checkpoint → WAL → swap → IDLE.

Runs the proxy in-process with aiohttp_client + respx mocking.
No real API calls. Verifies the full double-buffer lifecycle including
phase transitions, swap responses, WAL stitching, and state reset.
"""

from __future__ import annotations

import asyncio
import json

import pytest
import httpx
import respx
from httpx import Response

from dbproxy.buffer.state_machine import BufferPhase
from dbproxy.config import ProxyConfig
from dbproxy.identity.fingerprint import compute_fingerprint
from dbproxy.server import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def config():
    return ProxyConfig(
        host="127.0.0.1",
        port=0,
        upstream_url="https://api.anthropic.com",
        checkpoint_threshold=0.60,
        swap_threshold=0.80,
        db_path=":memory:",
        passthrough=False,
    )


@pytest.fixture
async def app(config):
    test_client = httpx.AsyncClient(
        base_url=config.upstream_url,
        http2=True,
        follow_redirects=True,
    )
    return await create_app(config, http_client=test_client)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Context window: 200_000 tokens (default for claude-sonnet-4-6)
# checkpoint_threshold: 0.60 → 120_000 tokens
# swap_threshold: 0.80 → 160_000 tokens

HEADERS = {"x-api-key": "test-key", "content-type": "application/json"}

# Stable request body — same fingerprint across all rounds.
_SYSTEM = "You are helpful."
_USER_MSG = {"role": "user", "content": "hello"}
_ASST_MSG = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}


def _body(messages=None, compact=False):
    """Build a /v1/messages request body with consistent fingerprint."""
    body = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 8192,
        "stream": False,
        "system": _SYSTEM,
        "messages": messages or [_USER_MSG],
    }
    if compact:
        body["context_management"] = {
            "edits": [
                {
                    "type": "compact_20260112",
                    "trigger": {"type": "input_tokens", "value": 100000},
                }
            ]
        }
    return body


def _api_response(input_tokens=1000, text="Hello!"):
    """Build a mock upstream response with specified token count."""
    return {
        "id": "msg_test123",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": input_tokens, "output_tokens": 50},
    }


def _checkpoint_response(summary="This is the checkpoint summary."):
    """Build a mock compaction response from the upstream API."""
    return {
        "id": "msg_checkpoint",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "compaction", "content": summary}],
        "model": "claude-sonnet-4-6",
        "stop_reason": "compaction",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def _get_mgr(client, body=None):
    """Retrieve the BufferManager for a test conversation.

    If body is None, returns the first (and usually only) manager.
    """
    registry = client.app["registry"]
    if body is not None:
        fp = compute_fingerprint(body)
        return registry.get(fp)
    # Return the single conversation in the registry
    convs = registry.all_conversations()
    if convs:
        return next(iter(convs.values()))
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    """Drive a conversation through the full checkpoint → swap lifecycle."""

    @respx.mock
    async def test_idle_to_checkpoint_to_swap_to_idle(self, client):
        """Full lifecycle: 5 rounds covering all phase transitions."""

        # Side effect dispatches checkpoint vs. normal requests by content.
        # Token count is determined by message count (each round has a unique count).
        def side_effect(request: httpx.Request) -> Response:
            body = json.loads(request.content)

            # Detect checkpoint request: has context_management with compact_20260112
            ctx_mgmt = body.get("context_management", {})
            edits = ctx_mgmt.get("edits", [])
            is_checkpoint = any(e.get("type") == "compact_20260112" for e in edits)
            if is_checkpoint:
                return Response(200, json=_checkpoint_response())

            # Normal request — escalating tokens keyed by message count.
            msg_count = len(body.get("messages", []))
            token_map = {
                1: 50_000,    # Round 1: 25% → IDLE
                3: 100_000,   # Round 2: 50% → IDLE
                5: 130_000,   # Round 3: 65% → checkpoint triggers
                7: 170_000,   # Round 4: 85% → SWAP_READY
            }
            input_tokens = token_map.get(msg_count, 180_000)
            return Response(200, json=_api_response(input_tokens=input_tokens))

        respx.post(path="/v1/messages").mock(side_effect=side_effect)

        # ---------------------------------------------------------------
        # Round 1: 1 message, 50k tokens (25%) → IDLE
        # ---------------------------------------------------------------
        resp = await client.post(
            "/v1/messages",
            json=_body(messages=[_USER_MSG]),
            headers=HEADERS,
        )
        assert resp.status == 200
        assert resp.headers["x-double-buffer-phase"] == "IDLE"

        mgr = _get_mgr(client)
        assert mgr is not None
        assert mgr.phase == BufferPhase.IDLE
        assert mgr.total_input_tokens == 50_000

        # ---------------------------------------------------------------
        # Round 2: 3 messages, 100k tokens (50%) → IDLE
        # ---------------------------------------------------------------
        resp = await client.post(
            "/v1/messages",
            json=_body(messages=[
                _USER_MSG, _ASST_MSG,
                {"role": "user", "content": "continue"},
            ]),
            headers=HEADERS,
        )
        assert resp.status == 200
        assert resp.headers["x-double-buffer-phase"] == "IDLE"
        assert mgr.total_input_tokens == 100_000

        # ---------------------------------------------------------------
        # Round 3: 5 messages, 130k tokens (65%) → checkpoint triggers
        # After background checkpoint completes: WAL_ACTIVE
        # ---------------------------------------------------------------
        resp = await client.post(
            "/v1/messages",
            json=_body(messages=[
                _USER_MSG, _ASST_MSG,
                {"role": "user", "content": "more"},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                {"role": "user", "content": "keep going"},
            ]),
            headers=HEADERS,
        )
        assert resp.status == 200

        # Give the background checkpoint task a moment to finalize
        await asyncio.sleep(0.1)

        assert mgr.checkpoint_content == "This is the checkpoint summary."
        assert mgr.phase == BufferPhase.WAL_ACTIVE

        # ---------------------------------------------------------------
        # Round 4: 7 messages, 170k tokens (85%) → SWAP_READY
        # ---------------------------------------------------------------
        resp = await client.post(
            "/v1/messages",
            json=_body(messages=[
                _USER_MSG, _ASST_MSG,
                {"role": "user", "content": "more"},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                {"role": "user", "content": "keep going"},
                {"role": "assistant", "content": [{"type": "text", "text": "sure"}]},
                {"role": "user", "content": "even more"},
            ]),
            headers=HEADERS,
        )
        assert resp.status == 200
        assert mgr.phase == BufferPhase.SWAP_READY

        # ---------------------------------------------------------------
        # Round 5: SWAP_READY → swap intercepts before forwarding.
        # Returns synthetic compaction response.
        # ---------------------------------------------------------------
        resp = await client.post(
            "/v1/messages",
            json=_body(messages=[
                _USER_MSG, _ASST_MSG,
                {"role": "user", "content": "more"},
                {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                {"role": "user", "content": "keep going"},
                {"role": "assistant", "content": [{"type": "text", "text": "sure"}]},
                {"role": "user", "content": "even more"},
                {"role": "assistant", "content": [{"type": "text", "text": "alright"}]},
                {"role": "user", "content": "final"},
            ]),
            headers=HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()

        # Swap response verification
        assert data["stop_reason"] == "compaction"
        assert data["content"][0]["type"] == "compaction"
        compaction_content = data["content"][0]["content"]

        # Compaction includes checkpoint summary
        assert "This is the checkpoint summary." in compaction_content
        # Compaction includes WAL (messages after anchor)
        assert "<recent_activity>" in compaction_content

        # State resets
        assert mgr.phase == BufferPhase.IDLE
        assert mgr.total_input_tokens == 0
        assert mgr.checkpoint_content is None


class TestPostSwapForwarding:
    """After swap, the client sends the compaction block back — verify forwarding."""

    @respx.mock
    async def test_compaction_block_stripped_to_text(self, client):
        """Compaction block in subsequent request is converted to text."""
        route = respx.post(path="/v1/messages").mock(
            return_value=Response(200, json=_api_response(input_tokens=5000)),
        )

        # Simulate post-swap: client sends compaction block back
        messages = [
            {
                "role": "assistant",
                "content": [{"type": "compaction", "content": "summary of conversation"}],
            },
            {"role": "user", "content": "continue working"},
        ]
        resp = await client.post(
            "/v1/messages",
            json=_body(messages=messages),
            headers=HEADERS,
        )
        assert resp.status == 200

        # Verify the forwarded request has compaction converted to text
        forwarded = json.loads(route.calls[0].request.content)
        first_content = forwarded["messages"][0]["content"]
        assert isinstance(first_content, list)
        assert first_content[0]["type"] == "text"
        assert first_content[0]["text"] == "summary of conversation"

        # Manager should have reset to IDLE (incoming compaction detected)
        mgr = _get_mgr(client)
        assert mgr is not None
        assert mgr.phase == BufferPhase.IDLE


class TestEmergencySwap:
    """When utilization jumps past both thresholds in one request."""

    @respx.mock
    async def test_emergency_skip_to_swap(self, client):
        """Jumping past both thresholds runs blocking checkpoint → SWAP_READY."""

        def side_effect(request: httpx.Request) -> Response:
            body = json.loads(request.content)
            ctx_mgmt = body.get("context_management", {})
            edits = ctx_mgmt.get("edits", [])
            is_checkpoint = any(e.get("type") == "compact_20260112" for e in edits)

            if is_checkpoint:
                return Response(200, json=_checkpoint_response("emergency summary"))

            # Single request at 90% utilization
            return Response(200, json=_api_response(input_tokens=180_000))

        respx.post(path="/v1/messages").mock(side_effect=side_effect)

        resp = await client.post(
            "/v1/messages",
            json=_body(),
            headers=HEADERS,
        )
        assert resp.status == 200

        mgr = _get_mgr(client)
        assert mgr is not None
        # Emergency path: IDLE → blocking checkpoint → SWAP_READY
        assert mgr.phase == BufferPhase.SWAP_READY
        assert mgr.checkpoint_content == "emergency summary"

        # Next request triggers swap
        resp = await client.post(
            "/v1/messages",
            json=_body(),
            headers=HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["stop_reason"] == "compaction"
        assert "emergency summary" in data["content"][0]["content"]
        assert mgr.phase == BufferPhase.IDLE


class TestWALStitching:
    """Verify WAL messages are stitched into compaction content."""

    @respx.mock
    async def test_wal_includes_post_anchor_messages(self, client):
        """WAL section should contain messages after the checkpoint anchor."""

        # We'll manually set up the state to test WAL stitching directly
        route = respx.post(path="/v1/messages").mock(
            return_value=Response(200, json=_api_response(input_tokens=5000)),
        )

        # Make initial request to register the conversation
        messages = [
            _USER_MSG,
            _ASST_MSG,
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": [{"type": "text", "text": "second answer"}]},
            {"role": "user", "content": "third question"},
        ]
        resp = await client.post(
            "/v1/messages",
            json=_body(messages=messages),
            headers=HEADERS,
        )
        assert resp.status == 200

        # Manually set up SWAP_READY with checkpoint at anchor=2 (first 2 messages)
        mgr = _get_mgr(client)
        assert mgr is not None
        mgr.phase = BufferPhase.SWAP_READY
        mgr.checkpoint_content = "Summary of first exchange."
        mgr.checkpoint_anchor_index = 2  # Messages [0:2] checkpointed

        # Next request triggers swap — should include WAL from index 2 onward
        resp = await client.post(
            "/v1/messages",
            json=_body(messages=messages),
            headers=HEADERS,
        )
        assert resp.status == 200
        data = await resp.json()

        assert data["stop_reason"] == "compaction"
        content = data["content"][0]["content"]

        # Checkpoint summary present
        assert "Summary of first exchange." in content
        # WAL section present with post-anchor messages
        assert "<recent_activity>" in content
        assert "second question" in content
        assert "second answer" in content
        assert "third question" in content

        # State reset
        assert mgr.phase == BufferPhase.IDLE
        assert mgr.total_input_tokens == 0
