"""Tiny CONNECT redirector for HTTPS_PROXY support.

Accepts HTTP CONNECT requests. If the target is api.anthropic.com:443,
redirects the TCP connection to the local main proxy (127.0.0.1:443).
All other targets are tunneled to the real destination (pass-through).

This is NOT MITM — the client does TLS directly with the endpoint.
"""

from __future__ import annotations

import asyncio
import os
import sys

import structlog

log = structlog.get_logger()

REDIRECT_HOST = "api.anthropic.com"
PROXY_TARGET = ("127.0.0.1", 443)
LISTEN_PORT = int(os.environ.get("SYNIX_REDIRECTOR_PORT", "8080"))
LISTEN_HOST = os.environ.get("SYNIX_REDIRECTOR_HOST", "0.0.0.0")
HEADER_TIMEOUT = 30  # seconds to read the CONNECT header
BUF_SIZE = 65536


async def _relay(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Relay bytes from reader to writer until EOF.

    Signals EOF via write_eof() rather than closing the connection,
    so the reverse relay can still deliver its remaining data.
    """
    try:
        while True:
            data = await reader.read(BUF_SIZE)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, OSError) as exc:
        log.debug("relay_closed", error=str(exc))
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except OSError:
            pass


async def handle_connect(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
) -> None:
    """Handle one incoming CONNECT request."""
    peer = client_writer.get_extra_info("peername")
    try:
        raw_line = await asyncio.wait_for(
            client_reader.readline(), timeout=HEADER_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.warning("connect_header_timeout", peer=peer)
        client_writer.close()
        return

    line = raw_line.decode("utf-8", errors="replace").strip()
    parts = line.split()
    method = parts[0].upper() if parts else ""

    if method != "CONNECT":
        log.warning("connect_not_connect_method", line=line, peer=peer)
        client_writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    # Parse: CONNECT host:port HTTP/1.x
    if len(parts) < 2:
        log.warning("connect_bad_request", line=line, peer=peer)
        client_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    target = parts[1]
    if ":" in target:
        host, port_str = target.rsplit(":", 1)
        port = int(port_str)
    else:
        host = target
        port = 443

    # Consume remaining headers until blank line
    while True:
        try:
            header_line = await asyncio.wait_for(
                client_reader.readline(), timeout=HEADER_TIMEOUT
            )
        except asyncio.TimeoutError:
            log.warning("connect_headers_timeout", peer=peer)
            client_writer.close()
            return
        if header_line in (b"\r\n", b"\n", b""):
            break

    # Decide where to connect
    if host == REDIRECT_HOST and port == 443:
        dest_host, dest_port = PROXY_TARGET
        log.info("connect_redirect", target=target, dest=f"{dest_host}:{dest_port}", peer=peer)
    else:
        dest_host, dest_port = host, port
        log.info("connect_passthrough", target=target, peer=peer)

    # Open upstream connection
    try:
        upstream_reader, upstream_writer = await asyncio.open_connection(
            dest_host, dest_port
        )
    except OSError as exc:
        log.error("connect_upstream_failed", target=target, dest=f"{dest_host}:{dest_port}", error=str(exc))
        client_writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        await client_writer.drain()
        client_writer.close()
        return

    # Tell client the tunnel is established
    client_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await client_writer.drain()

    # Bidirectional relay
    await asyncio.gather(
        _relay(client_reader, upstream_writer),
        _relay(upstream_reader, client_writer),
    )

    # Cleanup
    for w in (upstream_writer, client_writer):
        try:
            w.close()
        except OSError:
            pass


async def run_redirector(host: str = LISTEN_HOST, port: int = LISTEN_PORT) -> None:
    """Start the CONNECT redirector server."""
    server = await asyncio.start_server(handle_connect, host, port)
    addrs = [s.getsockname() for s in server.sockets]
    log.info("redirector_started", listen=addrs)
    async with server:
        await server.serve_forever()


def main() -> None:
    from .logging_config import setup_logging

    log_level = os.environ.get("SYNIX_LOG_LEVEL", "INFO")
    log_dir = os.environ.get("SYNIX_LOG_DIR", "logs")
    setup_logging(log_dir, log_level)
    try:
        asyncio.run(run_redirector())
    except KeyboardInterrupt:
        log.info("redirector_shutdown")
        sys.exit(0)


if __name__ == "__main__":
    main()
