"""Tests for the streamable HTTP transport and its bearer-token guard."""

from __future__ import annotations

import httpx
import pytest

from grocy_mcp.mcp.http_transport import build_http_app
from grocy_mcp.mcp.server import create_mcp_server

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream"}


async def _post(app, headers=None, path="/mcp", host="a0d7b954-grocy-mcp:8100"):
    transport = httpx.ASGITransport(app=app)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as client:
            return await client.post(path, json=INIT, headers={**HEADERS, **(headers or {})})


@pytest.fixture
def mcp():
    return create_mcp_server()


async def test_no_token_allows_requests(mcp):
    resp = await _post(build_http_app(mcp))
    assert resp.status_code == 200


async def test_missing_token_rejected(mcp):
    resp = await _post(build_http_app(mcp, token="s3cret"))
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


async def test_wrong_token_rejected(mcp):
    resp = await _post(build_http_app(mcp, token="s3cret"), {"Authorization": "Bearer nope"})
    assert resp.status_code == 401


async def test_correct_token_accepted(mcp):
    resp = await _post(build_http_app(mcp, token="s3cret"), {"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200


async def test_public_hostname_accepted(mcp):
    resp = await _post(
        build_http_app(mcp, token="s3cret"),
        {"Authorization": "Bearer s3cret"},
        host="grocy-mcp.example.com",
    )
    assert resp.status_code == 200


async def test_well_known_bypasses_auth(mcp):
    resp = await _post(
        build_http_app(mcp, token="s3cret"), path="/.well-known/oauth-protected-resource"
    )
    assert resp.status_code != 401


@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-protected-resource/mcp",
        "/.well-known/oauth-authorization-server/mcp",
        "/mcp/.well-known/oauth-authorization-server",
        "/mcp/.well-known/openid-configuration",
    ],
)
async def test_discovery_paths_never_401(mcp, path):
    """With OAuth off, discovery probes must 404, not 401 (a 401 makes clients assume OAuth)."""
    transport = httpx.ASGITransport(app=build_http_app(mcp, token="s3cret"))
    async with httpx.AsyncClient(transport=transport, base_url="http://h") as client:
        resp = await client.get(path)
    assert resp.status_code != 401, path


async def test_custom_path(mcp):
    resp = await _post(build_http_app(mcp, path="/grocy"), path="/grocy")
    assert resp.status_code == 200
