"""
Home Assistant app entrypoint.

Reads the app options from /data/options.json (written by the Supervisor),
maps them to the environment variables grocy-mcp expects, checks that Grocy
answers, then serves MCP over streamable HTTP. Outside Home Assistant (no
options file), plain environment variables are used as-is.
"""

import json
import os
import sys
from pathlib import Path

import httpx

OPTIONS_FILE = Path("/data/options.json")

print(f"Grocy MCP Server app v{os.environ.get('APP_VERSION', 'dev')}", flush=True)

if OPTIONS_FILE.exists():
    opts = json.loads(OPTIONS_FILE.read_text())
    mapping = {
        "grocy_url": "GROCY_URL",
        "grocy_api_key": "GROCY_API_KEY",
        "mcp_auth_token": "MCP_AUTH_TOKEN",
        "log_level": "MCP_LOG_LEVEL",
    }
    for key, env in mapping.items():
        value = opts.get(key)
        if value not in (None, ""):
            os.environ[env] = str(value).strip()

os.environ["GROCY_URL"] = os.environ.get("GROCY_URL", "").rstrip("/")

missing = [v for v in ("GROCY_URL", "GROCY_API_KEY") if not os.environ.get(v)]
if missing:
    print(
        f"ERROR: missing configuration: {', '.join(missing)}. "
        "Set them on the app's Configuration tab.",
        flush=True,
    )
    sys.exit(1)

if not os.environ.get("MCP_AUTH_TOKEN"):
    print(
        "WARNING: mcp_auth_token is empty - the MCP endpoint is open to "
        "anyone who can reach this port on your network.",
        flush=True,
    )

# Startup check. A failure is only a warning: Grocy may still be starting, and
# every tool call opens its own connection anyway.
url = os.environ["GROCY_URL"]
try:
    resp = httpx.get(
        f"{url}/api/system/info",
        headers={"GROCY-API-KEY": os.environ["GROCY_API_KEY"], "Accept": "application/json"},
        timeout=10,
    )
    if resp.status_code == 200:
        version = resp.json().get("grocy_version", {}).get("Version", "unknown")
        print(f"Connected to Grocy {version} at {url}", flush=True)
    elif resp.status_code == 401:
        print(
            f"WARNING: Grocy at {url} rejected the API key (401). Check grocy_api_key.", flush=True
        )
    else:
        print(
            f"WARNING: Grocy at {url} answered HTTP {resp.status_code} for /api/system/info. "
            "Check grocy_url (don't use an Ingress URL).",
            flush=True,
        )
except Exception as e:  # noqa: BLE001 - log and keep starting
    print(
        f"WARNING: Grocy not reachable at {url} ({e.__class__.__name__}: {e}). "
        "Tools will fail until it is.",
        flush=True,
    )

from grocy_mcp.mcp.http_transport import run_http  # noqa: E402
from grocy_mcp.mcp.server import create_mcp_server  # noqa: E402

run_http(create_mcp_server())
