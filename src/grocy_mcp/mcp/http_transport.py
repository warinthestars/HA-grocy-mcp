"""Long-running streamable HTTP transport with optional bearer-token or OAuth auth.

Used by the Home Assistant app (``grocy_mcp_app/run.py``). Kept separate from
``server.main`` so the fork stays easy to sync with upstream.

Environment variables:
    MCP_HOST        Bind address (default: 0.0.0.0)
    MCP_PORT        Listen port (default: 8100)
    MCP_PATH        Endpoint path (default: /mcp)
    MCP_AUTH_TOKEN  Optional bearer token. When set, every request must send
                    ``Authorization: Bearer <token>``.
    MCP_LOG_LEVEL   Uvicorn log level (default: info)
    MCP_UI_ENABLED  Set to 0 to turn the web UI off (see web_ui.py)
    MCP_OAUTH_*     FORK: optional OAuth sign-in (see oauth.py). When enabled,
                    FastMCP's auth handles the endpoint and still accepts
                    MCP_AUTH_TOKEN as a bearer token.
"""

from __future__ import annotations

import hmac
import inspect
import os
from typing import Any

from starlette.middleware import Middleware

DEFAULT_PORT = 8100


class BearerAuthMiddleware:
    """Minimal pure-ASGI bearer token check (safe for streaming responses)."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        # Let OAuth discovery probes (/.well-known/...) through unauthenticated so
        # they get a plain 404. Answering them with 401 makes clients such as
        # Claude's custom connectors assume OAuth is required instead of using the
        # static bearer header they were configured with.
        # FORK: the web UI (/ and /ui/...) is guarded separately in web_ui.py
        # (Home Assistant ingress or loopback only), so it skips the bearer check.
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            # Any discovery path, including the endpoint-relative form Gemini
            # probes (/mcp/.well-known/oauth-authorization-server).
            or "/.well-known/" in path
            or path == "/"
            or path.startswith("/ui/")
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")
        if not hmac.compare_digest(provided, self.expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
            return

        await self.app(scope, receive, send)


def build_http_app(mcp: Any, path: str = "/mcp", token: str = "") -> Any:
    """Build the Starlette app for ``mcp``, guarded by a bearer token when given."""
    middleware = [Middleware(BearerAuthMiddleware, token=token)] if token else []
    kwargs: dict[str, Any] = {"path": path, "middleware": middleware, "stateless_http": True}
    # The server is reached through Home Assistant hostnames, LAN IPs and tunnels,
    # so Host/Origin pinning would reject legitimate clients; the bearer token is
    # the access control. Older FastMCP releases don't have this option.
    if "host_origin_protection" in inspect.signature(mcp.http_app).parameters:
        kwargs["host_origin_protection"] = False
    return mcp.http_app(**kwargs)


def run_http(
    mcp: Any,
    host: str | None = None,
    port: int | None = None,
    path: str | None = None,
) -> None:
    """Serve ``mcp`` over streamable HTTP until the process is stopped."""
    import uvicorn

    host = host or os.getenv("MCP_HOST", "0.0.0.0")
    port = port or int(os.getenv("MCP_PORT", str(DEFAULT_PORT)))
    path = path or os.getenv("MCP_PATH", "/mcp")
    token = os.getenv("MCP_AUTH_TOKEN", "").strip()
    log_level = os.getenv("MCP_LOG_LEVEL", "info").lower()

    # FORK: web UI (Home Assistant "Open Web UI")
    try:
        from grocy_mcp.mcp.web_ui import register_ui

        register_ui(mcp)
    except Exception as e:  # the MCP endpoint must still start if the UI fails
        print(f"Web UI not available: {e}", flush=True)

    # FORK: OAuth sign-in (oauth.py). FastMCP's auth then guards the endpoint,
    # so the plain bearer middleware isn't used.
    oauth_on = False
    try:
        from grocy_mcp.mcp.oauth import GrocyOAuthProvider, config_problems, oauth_settings

        cfg = oauth_settings()
        if cfg["enabled"]:
            problems = config_problems(cfg)
            if problems:
                print(
                    "ERROR: OAuth is enabled but not configured: "
                    + " ".join(problems)
                    + " Continuing without OAuth.",
                    flush=True,
                )
            else:
                if cfg.get("public_url_corrected"):
                    print(
                        "WARNING: public_url should not include the endpoint path; "
                        f"using {cfg['public_url']}",
                        flush=True,
                    )
                mcp.auth = GrocyOAuthProvider(cfg)
                oauth_on = True
    except Exception as e:
        print(f"ERROR: OAuth not available: {e}. Continuing without OAuth.", flush=True)

    app = build_http_app(mcp, path=path, token="" if oauth_on else token)

    modes = [m for m, on in (("bearer token", token), ("OAuth", oauth_on)) if on]
    print(
        f"Grocy MCP server listening on http://{host}:{port}{path} "
        f"(auth: {' + '.join(modes) or 'NONE'})",
        flush=True,
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level)
