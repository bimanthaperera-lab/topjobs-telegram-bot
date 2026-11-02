"""Minimal HTTP health endpoint.

Hosts such as Render's free web services need the process to listen on $PORT,
and an uptime monitor (e.g. UptimeRobot) pinging it keeps the instance awake.
Answers 200 to GET/HEAD on / and /health, 404 otherwise. No dependencies.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_PATHS = {"/", "/health", "/healthz"}
_MAX_REQUEST_LINE = 2048


def _response(status: str, body: bytes, include_body: bool) -> bytes:
    head = (
        f"HTTP/1.1 {status}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).encode()
    return head + (body if include_body else b"")


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        line = await asyncio.wait_for(reader.readline(), timeout=5)
        parts = line[:_MAX_REQUEST_LINE].decode("latin-1").split()
        method, path = (parts[0], parts[1].split("?", 1)[0]) if len(parts) >= 2 else ("", "")
        if method in ("GET", "HEAD") and path in _PATHS:
            writer.write(_response("200 OK", b"ok\n", method == "GET"))
        else:
            writer.write(_response("404 Not Found", b"not found\n", method != "HEAD"))
        await writer.drain()
    except (asyncio.TimeoutError, ConnectionError, UnicodeDecodeError):
        pass
    finally:
        writer.close()


async def start_health_server(port: int, host: str = "0.0.0.0") -> asyncio.Server:
    server = await asyncio.start_server(_handle, host, port)
    log.info("Health endpoint listening on %s:%d", host, port)
    return server
