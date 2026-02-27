"""aiohttp application factory, routes, and passthrough proxy."""

from __future__ import annotations

import json
import os
import ssl
from typing import Any

import dns.resolver
import httpcore
import httpx
import structlog
from aiohttp import web

from .config import ProxyConfig
from .dashboard.broadcaster import Broadcaster
from .dashboard.ws_handler import websocket_handler
from .identity.registry import ConversationRegistry
from .proxy.handler import MessageHandler
from .store.db import Database
from .tls import create_server_ssl_context, generate_certs

log = structlog.get_logger()


def resolve_upstream_ip(hostname: str = "api.anthropic.com") -> str:
    """Resolve the real IP of the upstream API, bypassing /etc/hosts.

    Uses an explicit DNS query to Google/Cloudflare DNS to get the real IP
    even when /etc/hosts maps the hostname to 127.0.0.1.
    """
    resolver = dns.resolver.Resolver()
    resolver.nameservers = ["8.8.8.8", "1.1.1.1"]
    answers = resolver.resolve(hostname, "A")
    ip = str(answers[0])
    log.info("upstream_ip_resolved", hostname=hostname, ip=ip)
    return ip


class _DNSOverrideBackend(httpcore.AsyncNetworkBackend):
    """Network backend that overrides DNS for specific hostnames.

    Allows httpx to use the real hostname in URLs (correct TLS SNI)
    while connecting to a resolved IP address (bypassing /etc/hosts).
    """

    def __init__(self, overrides: dict[str, str]) -> None:
        self._overrides = overrides
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self, host: str, port: int, **kwargs: object
    ) -> httpcore.AsyncNetworkStream:
        resolved = self._overrides.get(host, host)
        return await self._backend.connect_tcp(resolved, port, **kwargs)

    async def connect_unix_socket(
        self, path: str, **kwargs: object
    ) -> httpcore.AsyncNetworkStream:
        return await self._backend.connect_unix_socket(path, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


async def create_app(
    config: ProxyConfig | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> web.Application:
    """Create and configure the aiohttp application.

    Args:
        config: Proxy configuration. Defaults to ProxyConfig().
        http_client: Optional pre-configured httpx client (for testing).
            If not provided, one is created with DNS override transport.
    """
    if config is None:
        config = ProxyConfig()

    app = web.Application()
    app["config"] = config

    from urllib.parse import urlparse
    parsed = urlparse(config.upstream_url)
    upstream_host = parsed.hostname or "api.anthropic.com"
    app["upstream_host"] = upstream_host

    if http_client is not None:
        app["http_client"] = http_client
    else:
        # Resolve upstream IP (bypassing /etc/hosts via external DNS)
        dns_overrides: dict[str, str] = {}
        try:
            upstream_ip = resolve_upstream_ip(upstream_host)
            dns_overrides[upstream_host] = upstream_ip
        except Exception:
            log.warning("dns_resolution_failed_using_direct", upstream_url=config.upstream_url)

        # HTTP client: URL uses real hostname (correct TLS SNI), but TCP
        # connects to resolved IP via custom network backend (bypasses /etc/hosts).
        # We use httpx.AsyncHTTPTransport for proper request type conversion,
        # then inject our DNS override into its internal connection pool.
        transport = httpx.AsyncHTTPTransport(http2=True)
        if dns_overrides:
            transport._pool._network_backend = _DNSOverrideBackend(dns_overrides)

        app["http_client"] = httpx.AsyncClient(
            base_url=config.upstream_url,
            transport=transport,
            follow_redirects=True,
        )

    # Conversation registry
    app["registry"] = ConversationRegistry(ttl_seconds=config.conversation_ttl_seconds)

    # Dashboard broadcaster
    app["broadcaster"] = Broadcaster()

    # Database
    db = Database(config.db_path)
    app["db"] = db

    # Message handler
    handler = MessageHandler(
        config=config,
        http_client=app["http_client"],
        registry=app["registry"],
        broadcaster=app["broadcaster"],
    )
    app["message_handler"] = handler

    # Wire up broadcaster to buffer managers
    broadcaster = app["broadcaster"]

    async def on_state_change(mgr: Any) -> None:
        await broadcaster.broadcast_state(mgr)

    # Store callback factory for new conversations
    app["on_state_change"] = on_state_change

    # Routes
    app.router.add_post("/v1/messages", handle_messages)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/v1/_reset", handle_reset)
    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/dashboard/ws", websocket_handler)
    app.router.add_get("/dashboard/api/conversation/{key:.+}", handle_conversation_detail)
    app.router.add_static(
        "/dashboard/static",
        os.path.join(os.path.dirname(__file__), "dashboard", "static"),
    )
    # Catch-all for other API paths → passthrough
    app.router.add_route("*", "/v1/{path:.*}", handle_passthrough)
    # Catch-all for /api/ paths (OAuth, settings, event logging, etc.)
    app.router.add_route("*", "/api/{path:.*}", handle_api_passthrough)

    # Lifecycle
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


async def on_startup(app: web.Application) -> None:
    """Initialize resources on startup."""
    db: Database = app["db"]
    await db.connect()
    log.info("server_started", config=app["config"].model_dump())


async def on_cleanup(app: web.Application) -> None:
    """Clean up resources on shutdown."""
    await app["http_client"].aclose()
    db: Database = app["db"]
    await db.close()
    log.info("server_stopped")


async def handle_messages(request: web.Request) -> web.StreamResponse:
    """POST /v1/messages — main proxy endpoint."""
    handler: MessageHandler = request.app["message_handler"]

    # Register state change callback for new conversations
    # (done here so the registry has it for newly created managers)
    orig_get_or_create = request.app["registry"].get_or_create

    def patched_get_or_create(fp: str, model: str, cw: int) -> Any:
        mgr = orig_get_or_create(fp, model, cw)
        if mgr._on_state_change is None:
            mgr.set_state_change_callback(request.app["on_state_change"])
        return mgr

    request.app["registry"].get_or_create = patched_get_or_create  # type: ignore[assignment]
    try:
        return await handler.handle(request)
    finally:
        request.app["registry"].get_or_create = orig_get_or_create  # type: ignore[assignment]


async def handle_health(request: web.Request) -> web.Response:
    """GET /health — health check endpoint."""
    registry: ConversationRegistry = request.app["registry"]
    return web.json_response({
        "status": "ok",
        "conversations": len(registry),
        "passthrough": request.app["config"].passthrough,
    })


async def handle_reset(request: web.Request) -> web.Response:
    """POST /v1/_reset — reset a conversation or all conversations."""
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        body = {}

    registry: ConversationRegistry = request.app["registry"]
    conv_id = body.get("conv_id")

    if conv_id:
        for fp, mgr in registry.all_conversations().items():
            if fp.startswith(conv_id):
                await mgr.reset("api_reset")
                return web.json_response({"status": "reset", "conv_id": conv_id})
        return web.json_response({"error": "conversation not found"}, status=404)
    else:
        for mgr in registry.all_conversations().values():
            await mgr.reset("api_reset_all")
        return web.json_response({"status": "reset_all", "count": len(registry)})


async def handle_conversation_detail(request: web.Request) -> web.Response:
    """GET /dashboard/api/conversation/{key} — conversation detail.

    Key is either 'conv_id:model' (new format) or just 'conv_id' (legacy).
    """
    key = request.match_info["key"]
    registry: ConversationRegistry = request.app["registry"]
    for reg_key, mgr in registry.all_conversations().items():
        if reg_key == key or reg_key.startswith(key):
            return web.json_response(mgr.to_detail_dict())
    return web.json_response({"error": "conversation not found"}, status=404)


async def handle_dashboard(request: web.Request) -> web.FileResponse:
    """GET /dashboard — serve dashboard HTML."""
    return web.FileResponse(
        os.path.join(os.path.dirname(__file__), "dashboard", "static", "index.html")
    )


async def _passthrough_to_upstream(
    request: web.Request, url: str,
) -> web.Response:
    """Forward a request to upstream and return the response."""
    http_client: httpx.AsyncClient = request.app["http_client"]

    body = await request.read()
    from dbproxy.proxy.handler import _build_upstream_headers
    headers = _build_upstream_headers(request, body)

    # Preserve query string
    if request.query_string:
        url = f"{url}?{request.query_string}"

    try:
        resp = await http_client.request(
            method=request.method,
            url=url,
            headers=headers,
            content=body if body else None,
            timeout=120.0,
        )
        # Filter out hop-by-hop headers from upstream response
        safe_headers = {
            k: v for k, v in resp.headers.items()
            if k.lower() not in ("transfer-encoding", "connection", "keep-alive")
        }
        return web.Response(
            body=resp.content,
            status=resp.status_code,
            headers=safe_headers,
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        log.error("passthrough_error", path=url, error=str(exc))
        return web.json_response(
            {"error": {"type": "proxy_error", "message": str(exc)}},
            status=502,
        )


async def handle_passthrough(request: web.Request) -> web.Response:
    """Passthrough handler for non-/v1/messages API paths."""
    path = request.match_info.get("path", "")
    return await _passthrough_to_upstream(request, f"/v1/{path}")


async def handle_api_passthrough(request: web.Request) -> web.Response:
    """Passthrough handler for /api/ paths (OAuth, settings, etc.)."""
    path = request.match_info.get("path", "")
    return await _passthrough_to_upstream(request, f"/api/{path}")


def run_server(config: ProxyConfig | None = None) -> None:
    """Run the proxy server (blocking)."""
    import asyncio

    if config is None:
        config = ProxyConfig()

    async def _run() -> None:
        app = await create_app(config)

        # TLS setup
        _, cert_path, key_path = generate_certs(config.tls_ca_dir)
        ssl_ctx = create_server_ssl_context(cert_path, key_path)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(
            runner,
            host=config.host,
            port=config.port,
            ssl_context=ssl_ctx,
        )
        await site.start()
        log.info("server_listening", host=config.host, port=config.port, tls=True)

        # Run forever
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    asyncio.run(_run())
