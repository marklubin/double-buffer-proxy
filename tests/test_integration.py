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
        body["context_management"] = {
            "edits": [
                {
                    "type": "compact_20260112",
                    "trigger": {"type": "input_tokens", "value": 100000},
                }
            ]
        }
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


class TestCompactStripping:
    @pytest.mark.asyncio
    @respx.mock
    async def test_compact_edit_stripped(self, client):
        """Verify compact edit is stripped before forwarding."""
        mock_resp = _mock_api_response()
        route = _mock_upstream(mock_resp)

        body = _make_messages_request(compact=True)
        resp = await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert resp.status == 200

        # Verify the forwarded request had compact stripped
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


class TestSwapExecution:
    @pytest.mark.asyncio
    @respx.mock
    async def test_swap_returns_compaction(self, client):
        """When buffer is SWAP_READY, the next request should get a synthetic compaction."""
        # First, make a normal request to register the conversation
        _mock_upstream()

        body = _make_messages_request()
        await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )

        # Now manually set the manager to SWAP_READY with checkpoint content
        registry = client.app["registry"]
        from dbproxy.identity.fingerprint import compute_fingerprint
        fp = compute_fingerprint(body)
        mgr = registry.get(fp)
        assert mgr is not None

        from dbproxy.buffer.state_machine import BufferPhase
        mgr.phase = BufferPhase.SWAP_READY
        mgr.checkpoint_content = "This is the checkpoint summary"

        # Next request should trigger swap
        resp = await client.post(
            "/v1/messages",
            json=body,
            headers={"x-api-key": "test-key", "content-type": "application/json"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["stop_reason"] == "compaction"
        assert data["content"][0]["type"] == "compaction"
        assert data["content"][0]["content"] == "This is the checkpoint summary"

        # Manager should be back to IDLE
        assert mgr.phase == BufferPhase.IDLE
