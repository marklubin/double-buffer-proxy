"""POST /v1/messages handler — central proxy orchestrator.

Intercepts requests to the Anthropic messages API, applies double-buffer
logic, and forwards to upstream or returns synthetic responses.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import structlog
from aiohttp import web

from dbproxy.buffer.manager import BufferManager
from dbproxy.buffer.state_machine import BufferPhase
from dbproxy.buffer.swap import serialize_swap_response_bytes
from dbproxy.config import ProxyConfig
from dbproxy.identity.fingerprint import compute_fingerprint
from dbproxy.identity.registry import ConversationRegistry
from dbproxy.proxy.request_rewriter import (
    extract_request_metadata,
    has_compact_edit,
    has_compaction_block,
    strip_compact_edit,
    strip_compaction_blocks,
)
from dbproxy.proxy.sse_forwarder import SSEForwarder

log = structlog.get_logger()

# Headers to forward from client to upstream API.
# Whitelist approach avoids forwarding hop-by-hop or proxy-internal headers.
_FORWARD_HEADERS = {
    "x-api-key",
    "authorization",
    "content-type",
    "anthropic-version",
    "anthropic-beta",
    "anthropic-dangerous-direct-browser-access",
    "accept",
    "accept-encoding",
}


def _build_upstream_headers(request: web.Request, body_bytes: bytes) -> dict[str, str]:
    """Build headers for the upstream request, forwarding only safe headers."""
    headers: dict[str, str] = {}
    for key, value in request.headers.items():
        if key.lower() in _FORWARD_HEADERS:
            headers[key] = value
    headers["content-length"] = str(len(body_bytes))
    return headers


def _is_suggestion_request(body: dict[str, Any]) -> bool:
    """Detect Claude Code suggestion-mode requests.

    These are ephemeral requests that include '[SUGGESTION MODE:' in the
    last user message.  They should not update conversation state.
    """
    messages = body.get("messages", [])
    if not messages:
        return False
    last = messages[-1]
    if last.get("role") != "user":
        return False
    content = last.get("content", "")
    if isinstance(content, str):
        return "[SUGGESTION MODE:" in content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                if "[SUGGESTION MODE:" in block.get("text", ""):
                    return True
            elif isinstance(block, str) and "[SUGGESTION MODE:" in block:
                return True
    return False


class MessageHandler:
    """Handles /v1/messages requests with double-buffer logic."""

    def __init__(
        self,
        config: ProxyConfig,
        http_client: httpx.AsyncClient,
        registry: ConversationRegistry,
        broadcaster: Any | None = None,
    ) -> None:
        self.config = config
        self.http_client = http_client
        self.registry = registry
        self.broadcaster = broadcaster

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """Handle a POST /v1/messages request."""
        body_bytes = await request.read()
        try:
            body = json.loads(body_bytes)
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("request_parse_error", error=str(exc))
            return web.json_response(
                {"error": {"type": "invalid_request", "message": str(exc)}},
                status=400,
            )

        # Extract metadata
        metadata = extract_request_metadata(body)
        model = metadata["model"]
        stream = metadata["stream"]

        # Capture auth headers for checkpoint calls
        auth_headers: dict[str, str] = {}
        for key, value in request.headers.items():
            lk = key.lower()
            if lk in ("x-api-key", "authorization", "anthropic-version", "anthropic-beta"):
                auth_headers[lk] = value
        # Preserve the query string (e.g. ?beta=true) for checkpoint requests
        auth_headers["_query_string"] = request.query_string or ""

        # Fingerprint the conversation
        fingerprint = compute_fingerprint(body)
        context_window = self.config.context_window_for(model)
        mgr = self.registry.get_or_create(fingerprint, model, context_window)
        mgr.checkpoint_threshold = self.config.checkpoint_threshold
        mgr.swap_threshold = self.config.swap_threshold
        mgr.compact_trigger_tokens = self.config.compact_trigger_tokens

        log.info(
            "request_received",
            conv_id=fingerprint[:16],
            model=model,
            stream=stream,
            phase=mgr.phase.value,
            message_count=len(metadata["messages"]),
        )

        # Detect suggestion-mode requests (ephemeral — skip buffer logic)
        if _is_suggestion_request(body):
            log.debug("suggestion_request_passthrough", conv_id=fingerprint[:16])
            return await self._forward_request(request, body, body_bytes, mgr, stream)

        # Check for incoming compaction block → reset state
        if has_compaction_block(body):
            log.info("incoming_compaction_detected", conv_id=fingerprint[:16])
            await mgr.reset("incoming_compaction")

        # Update manager with current request context
        mgr.update_from_request(body, auth_headers)

        # Passthrough mode — skip all buffer logic
        if self.config.passthrough:
            return await self._forward_request(request, body, body_bytes, mgr, stream)

        # Check if we should execute a swap — either already SWAP_READY,
        # or WAL_ACTIVE with utilization past swap threshold (don't waste
        # a round-trip forwarding when we know we need to swap).
        if mgr.should_swap():
            return await self._execute_swap(mgr, stream)
        if (mgr.phase == BufferPhase.WAL_ACTIVE
                and mgr.checkpoint_content
                and mgr.utilization >= mgr.swap_threshold):
            log.info(
                "immediate_swap",
                conv_id=mgr.conv_id[:16],
                utilization=f"{mgr.utilization:.1%}",
            )
            mgr.phase = BufferPhase.SWAP_READY
            return await self._execute_swap(mgr, stream)

        # Check if this is a client-initiated compact
        client_wants_compact = has_compact_edit(body)
        if client_wants_compact:
            synthetic = await mgr.handle_client_compact(
                stream=stream,
                http_client=self.http_client,
                upstream_url=self.config.upstream_url,
            )
            if synthetic is not None:
                return self._send_synthetic_response(synthetic, mgr, stream)
            # Otherwise, forward the native compact request

        # Strip compact edit and forward
        rewritten_body = strip_compact_edit(body)
        return await self._forward_request(
            request, rewritten_body, json.dumps(rewritten_body).encode(), mgr, stream,
        )

    async def _forward_request(
        self,
        request: web.Request,
        body: dict[str, Any],
        body_bytes: bytes,
        mgr: BufferManager,
        stream: bool,
    ) -> web.StreamResponse:
        """Forward request to upstream API and handle response."""
        # Strip compaction blocks — convert to text so API accepts them
        if has_compaction_block(body):
            body = strip_compaction_blocks(body)
            body_bytes = json.dumps(body).encode()

        headers = _build_upstream_headers(request, body_bytes)

        # Use relative path so httpx uses its base_url (resolved IP)
        # Preserve query string (e.g. ?beta=true)
        upstream_path = "/v1/messages"
        if request.query_string:
            upstream_path = f"{upstream_path}?{request.query_string}"

        log.debug("request_forwarded", conv_id=mgr.conv_id[:16], path=upstream_path)

        try:
            if stream:
                return await self._forward_streaming(
                    request, upstream_path, headers, body_bytes, mgr,
                )
            else:
                return await self._forward_non_streaming(
                    upstream_path, headers, body_bytes, mgr,
                )
        except httpx.HTTPStatusError as exc:
            # Read the response body — may not be available on streaming responses
            try:
                error_body = exc.response.text[:500]
                response_content = exc.response.content
            except httpx.ResponseNotRead:
                await exc.response.aread()
                error_body = exc.response.text[:500]
                response_content = exc.response.content
            log.error(
                "upstream_error",
                conv_id=mgr.conv_id[:16],
                status=exc.response.status_code,
                body=error_body,
            )
            if self.broadcaster:
                await self.broadcaster.broadcast_error(
                    mgr.conv_id, exc.response.status_code, error_body,
                )
            return web.Response(
                body=response_content,
                status=exc.response.status_code,
                content_type="application/json",
            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            log.error("upstream_connection_error", conv_id=mgr.conv_id[:16], error=str(exc))
            if self.broadcaster:
                await self.broadcaster.broadcast_error(mgr.conv_id, 502, str(exc))
            return web.json_response(
                {"error": {"type": "proxy_error", "message": f"Upstream connection failed: {exc}"}},
                status=502,
            )

    async def _forward_streaming(
        self,
        original_request: web.Request,
        url: str,
        headers: dict[str, str],
        body_bytes: bytes,
        mgr: BufferManager,
    ) -> web.StreamResponse:
        """Forward a streaming request and pipe SSE events."""
        forwarder = SSEForwarder(conv_id=mgr.conv_id)

        async with self.http_client.stream(
            "POST",
            url,
            headers=headers,
            content=body_bytes,
            timeout=600.0,
        ) as upstream_response:
            if upstream_response.status_code >= 400:
                # Read error body while response is still open
                await upstream_response.aread()
                error_body = upstream_response.text[:500]
                log.error(
                    "upstream_error",
                    conv_id=mgr.conv_id[:16],
                    status=upstream_response.status_code,
                    body=error_body,
                )
                if self.broadcaster:
                    await self.broadcaster.broadcast_error(
                        mgr.conv_id, upstream_response.status_code, error_body,
                    )
                return web.Response(
                    body=upstream_response.content,
                    status=upstream_response.status_code,
                    content_type="application/json",
                )

            # Start client response
            client_response = web.StreamResponse(
                status=upstream_response.status_code,
                headers={
                    "content-type": "text/event-stream",
                    "cache-control": "no-cache",
                    "x-double-buffer-phase": mgr.phase.value,
                    "x-double-buffer-conv-id": mgr.conv_id[:16],
                },
            )
            await client_response.prepare(original_request)

            # Forward SSE stream
            await forwarder.forward_stream(
                upstream_response.aiter_bytes(),
                client_response,
                max_buffer_bytes=self.config.max_sse_buffer_bytes,
            )

        # Update token tracking
        if forwarder.usage:
            mgr.update_tokens(forwarder.usage)
            if self.broadcaster:
                await self.broadcaster.broadcast_state(mgr)

        # Check if upstream returned a compaction
        if forwarder.has_compaction:
            await mgr.reset("upstream_compaction")
        else:
            # Evaluate thresholds for double-buffer transitions
            await mgr.evaluate_thresholds(self.http_client, self.config.upstream_url)

        await client_response.write_eof()
        return client_response

    async def _forward_non_streaming(
        self,
        url: str,
        headers: dict[str, str],
        body_bytes: bytes,
        mgr: BufferManager,
    ) -> web.Response:
        """Forward a non-streaming request."""
        upstream_response = await self.http_client.post(
            url,
            headers=headers,
            content=body_bytes,
            timeout=600.0,
        )
        upstream_response.raise_for_status()

        response_data = upstream_response.json()

        # Update token tracking
        usage = response_data.get("usage", {})
        if usage:
            mgr.update_tokens(usage)
            if self.broadcaster:
                await self.broadcaster.broadcast_state(mgr)

        # Check for compaction in response
        content = response_data.get("content", [])
        has_compaction = any(
            b.get("type") == "compaction" for b in content if isinstance(b, dict)
        )

        if has_compaction:
            await mgr.reset("upstream_compaction")
        else:
            await mgr.evaluate_thresholds(self.http_client, self.config.upstream_url)

        return web.Response(
            body=upstream_response.content,
            status=upstream_response.status_code,
            content_type="application/json",
            headers={
                "x-double-buffer-phase": mgr.phase.value,
                "x-double-buffer-conv-id": mgr.conv_id[:16],
            },
        )

    async def _execute_swap(
        self,
        mgr: BufferManager,
        stream: bool,
    ) -> web.StreamResponse:
        """Execute a buffer swap, returning synthetic compaction to client."""
        response = await mgr.execute_swap(stream)
        return self._send_synthetic_response(response, mgr, stream)

    def _send_synthetic_response(
        self,
        response: dict[str, Any] | list,
        mgr: BufferManager,
        stream: bool,
    ) -> web.Response:
        """Send a synthetic compaction response to the client."""
        response_bytes = serialize_swap_response_bytes(response, stream)

        if stream:
            content_type = "text/event-stream"
        else:
            content_type = "application/json"

        log.info(
            "synthetic_response_sent",
            conv_id=mgr.conv_id[:16],
            stream=stream,
            bytes=len(response_bytes),
        )

        return web.Response(
            body=response_bytes,
            status=200,
            content_type=content_type,
            headers={
                "x-double-buffer-phase": mgr.phase.value,
                "x-double-buffer-conv-id": mgr.conv_id[:16],
            },
        )
