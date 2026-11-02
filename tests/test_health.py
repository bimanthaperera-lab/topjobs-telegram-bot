import asyncio

import pytest

from jobbot.health import start_health_server


async def request(port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


@pytest.fixture
async def port():
    server = await start_health_server(0, host="127.0.0.1")
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


@pytest.mark.parametrize("path", ["/", "/health", "/healthz", "/health?x=1"])
async def test_get_health_ok(port, path):
    resp = await request(port, f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert resp.endswith(b"ok\n")


async def test_head_has_no_body(port):
    resp = await request(port, b"HEAD /health HTTP/1.1\r\nHost: x\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 200 OK")
    assert resp.endswith(b"\r\n\r\n")


async def test_unknown_path_404(port):
    resp = await request(port, b"GET /admin HTTP/1.1\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 404")


async def test_post_rejected(port):
    resp = await request(port, b"POST /health HTTP/1.1\r\n\r\n")
    assert resp.startswith(b"HTTP/1.1 404")
