"""Tests for the CONNECT redirector."""

from __future__ import annotations

import asyncio

import pytest

from dbproxy.connect_redirector import handle_connect, REDIRECT_HOST


@pytest.fixture
async def echo_server():
    """Start a TCP echo server that sends back whatever it receives."""
    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        data = await reader.read(4096)
        writer.write(data)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port


@pytest.fixture
async def redirector(echo_server, monkeypatch):
    """Start the redirector, configured to redirect to the echo server."""
    monkeypatch.setattr(
        "dbproxy.connect_redirector.PROXY_TARGET", ("127.0.0.1", echo_server)
    )
    server = await asyncio.start_server(handle_connect, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port


async def _connect_through(proxy_port: int, target: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Send a CONNECT request through the redirector and return streams after 200."""
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    await writer.drain()
    response = await asyncio.wait_for(reader.readline(), timeout=5)
    assert b"200" in response, f"Expected 200, got: {response}"
    # Consume remaining header lines
    while True:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        if line in (b"\r\n", b"\n", b""):
            break
    return reader, writer


class TestConnectRedirector:
    async def test_redirect_api_anthropic(self, redirector):
        """CONNECT api.anthropic.com:443 should be redirected to the echo server."""
        reader, writer = await _connect_through(redirector, f"{REDIRECT_HOST}:443")
        writer.write(b"hello from client")
        await writer.drain()
        writer.write_eof()
        data = await asyncio.wait_for(reader.read(4096), timeout=5)
        assert data == b"hello from client"
        writer.close()

    async def test_non_connect_method_rejected(self, redirector):
        """Non-CONNECT methods should get 405."""
        reader, writer = await asyncio.open_connection("127.0.0.1", redirector)
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        assert b"405" in response
        writer.close()

    async def test_bad_request(self, redirector):
        """Malformed CONNECT line should get 400."""
        reader, writer = await asyncio.open_connection("127.0.0.1", redirector)
        writer.write(b"CONNECT\r\n\r\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        assert b"400" in response
        writer.close()

    async def test_unreachable_target_returns_502(self, redirector, monkeypatch):
        """CONNECT to unreachable host should get 502."""
        # Point at a port nothing is listening on
        monkeypatch.setattr(
            "dbproxy.connect_redirector.PROXY_TARGET", ("127.0.0.1", 1)
        )
        reader, writer = await asyncio.open_connection("127.0.0.1", redirector)
        writer.write(f"CONNECT {REDIRECT_HOST}:443 HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        response = await asyncio.wait_for(reader.read(4096), timeout=5)
        assert b"502" in response
        writer.close()
