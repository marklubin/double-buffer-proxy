"""Integration tests for the proxy server with mock upstream."""

import json

import pytest
import httpx
import respx
from httpx import Response

from dbproxy.config import ProxyConfig
from dbproxy.server import create_app


@pytest.fixture
def config():
    return ProxyConfig(
        host="127.0.0.1",
        port=0,  # Random port
        upstream_url="https://api.anthropic.com",
        checkpoint_threshold=0.70,
        swap_threshold=0.95,
        db_path=":memory:",
        passthrough=False,
    )


@pytest.fixture
async def app(config):
    # Use a plain httpx client (no custom transport) so respx can intercept
    test_client = httpx.AsyncClient(
        base_url=config.upstream_url,
        http2=True,
        follow_redirects=True,
    )
    return await create_app(config, http_client=test_client)


@pytest.fixture
async def client(aiohttp_client, app):
    return await aiohttp_client(app)


# The prompt Claude Code sends when requesting compaction.
_COMPACT_PROMPT = (
    "Your task is to create a detailed summary of the conversation so far, "
    "paying close attention to the user's explicit requests and intentions."
)


def _make_messages_request(
    messages=None,
    model="claude-sonnet-4-6",
    stream=False,
    system="You are helpful",
    compact=False,
):
    body = {
        "model": model,
        "max_tokens": 8192,
        "stream": stream,
        "system": system,
        "messages": messages or [{"role": "user", "content": "hello"}],
    }
    if compact:
        msgs = list(body["messages"])
        # Maintain proper alternation: if last msg is user, add assistant reply
        if msgs and msgs[-1].get("role") == "user":
            msgs.append({"role": "assistant", "content": [{"type": "text", "text": "OK."}]})
        msgs.append({"role": "user", "content": _COMPACT_PROMPT})
        body["messages"] = msgs
    return body


def _mock_api_response(
    content_text="Hello!",
    input_tokens=1000,
    output_tokens=50,
):
    return {
        "id": "msg_test123",
        "type": "message",
        "role": "assistant",
        "content": [{"type": "text", "text": content_text}],
        "model": "claude-sonnet-4-6",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _mock_upstream(mock_resp=None):
    """Mock any POST to /v1/messages regardless of host (matches resolved IP)."""
    if mock_resp is None:
        mock_resp = _mock_api_response()
    return respx.post(path="/v1/messages").mock(
        return_value=Response(200, json=mock_resp)
    )


class TestHealthEndpoint:
    @pytest.mark.asyncio
    async def test_health(self, client):
        resp = await client.get("/health")
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "ok"


class TestPassthrough:
    @pytest.mark.asyncio
    @respx.mock
    async def test_non_streaming_passthrough(self, client):
        mock_resp = _mock_api_response()
        _mock_upstream(mock_resp)

        body = _make_messages_request()
        resp = await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["content"][0]["text"] == "Hello!"


class TestCompactForwarding:
    @pytest.mark.asyncio
    @respx.mock
    async def test_compact_prompt_preserved_for_native_forward(self, client):
        """When no checkpoint available, compact request is forwarded with
        the compact prompt intact so the API generates the summary."""
        route = respx.post(path="/v1/messages").mock(
            return_value=Response(200, json=_mock_api_response(
                content_text="Here is a summary of the conversation."
            )),
        )

        body = _make_messages_request(compact=True)
        resp = await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert resp.status == 200

        # Verify the forwarded request has the compact prompt intact
        forwarded_body = json.loads(route.calls[0].request.content)
        last_msg = forwarded_body["messages"][-1]
        assert last_msg["role"] == "user"
        assert "create a detailed summary of the conversation" in last_msg["content"].lower()

    @pytest.mark.asyncio
    @respx.mock
    async def test_compact_edit_stripped_from_non_compact_request(self, client):
        """Non-compact requests have any stray compact edits stripped."""
        mock_resp = _mock_api_response()
        route = _mock_upstream(mock_resp)

        # Normal request (no compact=True) — compact edit should be stripped
        body = _make_messages_request()
        resp = await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert resp.status == 200

        # Verify no context_management in forwarded request
        forwarded_body = json.loads(route.calls[0].request.content)
        assert "context_management" not in forwarded_body


class TestBufferHeaders:
    @pytest.mark.asyncio
    @respx.mock
    async def test_response_has_buffer_headers(self, client):
        _mock_upstream()

        body = _make_messages_request()
        resp = await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert "x-double-buffer-phase" in resp.headers
        assert "x-double-buffer-conv-id" in resp.headers


class TestResetEndpoint:
    @pytest.mark.asyncio
    async def test_reset_all(self, client):
        resp = await client.post("/v1/_reset", json={})
        assert resp.status == 200
        data = await resp.json()
        assert data["status"] == "reset_all"

    @pytest.mark.asyncio
    async def test_reset_nonexistent(self, client):
        resp = await client.post("/v1/_reset", json={"conv_id": "nonexistent"})
        assert resp.status == 404


class TestClientCompactExecution:
    @pytest.mark.asyncio
    @respx.mock
    async def test_client_compact_with_checkpoint_returns_synthetic(self, client):
        """When checkpoint is ready, client compact request returns pre-computed summary."""
        _mock_upstream()

        body = _make_messages_request()
        await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )

        # Manually set the manager to SWAP_READY with checkpoint content
        registry = client.app["registry"]
        from dbproxy.identity.fingerprint import compute_fingerprint
        fp = compute_fingerprint(body)
        mgr = registry.get(fp)
        assert mgr is not None

        from dbproxy.buffer.state_machine import BufferPhase
        mgr.phase = BufferPhase.SWAP_READY
        mgr.checkpoint_content = "This is the checkpoint summary"

        # Client sends compact request → proxy intercepts with pre-computed checkpoint
        compact_body = _make_messages_request(compact=True)
        resp = await client.post(
            "/v1/messages",
            json=compact_body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["stop_reason"] == "end_turn"
        assert data["content"][0]["type"] == "text"
        assert data["content"][0]["text"] == "This is the checkpoint summary"

        # Manager should be back to IDLE
        assert mgr.phase == BufferPhase.IDLE

    @pytest.mark.asyncio
    @respx.mock
    async def test_normal_request_forwarded_when_swap_ready(self, client):
        """When SWAP_READY, a normal (non-compact) request is forwarded normally."""
        _mock_upstream()

        body = _make_messages_request()
        await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )

        # Set SWAP_READY
        registry = client.app["registry"]
        from dbproxy.identity.fingerprint import compute_fingerprint
        fp = compute_fingerprint(body)
        mgr = registry.get(fp)
        assert mgr is not None

        from dbproxy.buffer.state_machine import BufferPhase
        mgr.phase = BufferPhase.SWAP_READY
        mgr.checkpoint_content = "This is the checkpoint summary"

        # Normal request (no compact) — should be forwarded, not intercepted
        resp = await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert resp.status == 200
        data = await resp.json()
        # Should be a normal response, NOT compaction
        assert data["stop_reason"] == "end_turn"
        assert data["content"][0]["type"] == "text"

    @pytest.mark.asyncio
    @respx.mock
    async def test_client_compact_no_checkpoint_forwards_native(self, client):
        """When no checkpoint available, compact request is forwarded to API natively."""
        # Mock upstream to return a regular text response (as the API does for compact)
        summary_resp = _mock_api_response(
            content_text="Summary of the conversation so far.",
            input_tokens=1000,
        )
        route = respx.post(path="/v1/messages").mock(
            return_value=Response(200, json=summary_resp),
        )

        # Send compact request when in IDLE (no checkpoint)
        compact_body = _make_messages_request(compact=True)
        resp = await client.post(
            "/v1/messages",
            json=compact_body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert resp.status == 200

        # Verify the forwarded request has the compact prompt intact
        forwarded = json.loads(route.calls[0].request.content)
        last_msg = forwarded["messages"][-1]
        assert last_msg["role"] == "user"
        assert "create a detailed summary of the conversation" in last_msg["content"].lower()

    @pytest.mark.asyncio
    @respx.mock
    async def test_native_compact_resets_manager(self, client):
        """After forwarding a native compact request, manager resets to IDLE."""
        route = respx.post(path="/v1/messages").mock(
            return_value=Response(200, json=_mock_api_response(
                content_text="Summary.", input_tokens=1000,
            )),
        )

        # First request to register conversation
        body = _make_messages_request()
        await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )

        # Set manager to WAL_ACTIVE (has checkpoint but hasn't reached swap threshold)
        registry = client.app["registry"]
        from dbproxy.identity.fingerprint import compute_fingerprint
        from dbproxy.buffer.state_machine import BufferPhase
        fp = compute_fingerprint(body)
        mgr = registry.get(fp)
        assert mgr is not None
        mgr.phase = BufferPhase.WAL_ACTIVE
        mgr.checkpoint_content = "old checkpoint"

        # Send compact request — should forward natively (WAL_ACTIVE → handle_client_compact
        # promotes to SWAP_READY, then execute_swap returns synthetic)
        compact_body = _make_messages_request(compact=True)
        resp = await client.post(
            "/v1/messages",
            json=compact_body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert resp.status == 200

        # Manager resets after swap
        assert mgr.phase == BufferPhase.IDLE
