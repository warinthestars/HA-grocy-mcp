"""
FORK: Web UI for the Grocy MCP server (Home Assistant "Open Web UI" / ingress).

Mirrors the Mealie MCP app's web UI. Pages: Overview (server + Grocy status and
enabled Grocy features), Connect (client setup snippets), Tools (enable/disable
MCP tools, persisted), Library (counts of Grocy records), Attention (expired,
overdue and due-soon stock, products below minimum stock, overdue chores, tasks
and batteries).

Access control:
- Inside the Home Assistant app, the UI only answers requests proxied by the
  Supervisor's ingress (peer 172.30.32.2); HA has already authenticated the user.
- Elsewhere it answers loopback requests only, unless MCP_UI_ALLOW_REMOTE=1.
- MCP_UI_ENABLED=0 turns the UI off.
The MCP endpoint itself keeps its bearer-token protection (see http_transport).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

log = logging.getLogger("grocy_mcp.ui")

SUPERVISOR_INGRESS_IP = "172.30.32.2"
UI_PREFIX = "/ui/"
_STARTED = time.time()
_STATE: dict[str, Any] = {"tools": {}, "disabled": set()}

# (tool-name prefix, category, Grocy feature flag the tools depend on). First match wins,
# so the specific print_* / workflow_* prefixes come before the generic ones.
CATEGORY_RULES: list[tuple[str, str, str | None]] = [
    ("print_recipe_label", "Recipes", "RECIPES"),
    ("print_chore_label", "Chores", "CHORES"),
    ("print_battery_label", "Batteries", "BATTERIES"),
    ("print_shopping_list", "Shopping lists", "SHOPPINGLIST"),
    ("print_", "Stock", "STOCK"),
    ("workflow_shopping_", "Shopping lists", "SHOPPINGLIST"),
    ("workflow_", "Stock", "STOCK"),
    ("stock_", "Stock", "STOCK"),
    ("location", "Stock", "STOCK"),
    ("shopping_", "Shopping lists", "SHOPPINGLIST"),
    ("recipe", "Recipes", "RECIPES"),
    # Grocy's meal plan is part of its recipes feature (no separate flag).
    ("meal_plan_", "Meal plan", "RECIPES"),
    ("chore", "Chores", "CHORES"),
    ("task", "Tasks", "TASKS"),
    ("batter", "Batteries", "BATTERIES"),
    ("equipment_", "Equipment", "EQUIPMENT"),
    ("calendar_", "Calendar", "CALENDAR"),
    ("catalog_", "Catalog & entities", None),
    ("entity_", "Catalog & entities", None),
    ("describe_", "Catalog & entities", None),
    ("discover_", "Catalog & entities", None),
    ("file_", "Files", None),
    ("system_", "System", None),
]

FEATURES = [
    "STOCK",
    "SHOPPINGLIST",
    "RECIPES",
    "CHORES",
    "TASKS",
    "BATTERIES",
    "EQUIPMENT",
    "CALENDAR",
]

# Library tab: (key, Grocy entity, optional query filter)
LIBRARY_ENTITIES: list[tuple[str, str, str | None]] = [
    ("products", "products", None),
    ("locations", "locations", None),
    ("product_groups", "product_groups", None),
    ("quantity_units", "quantity_units", None),
    ("barcodes", "product_barcodes", None),
    ("shopping_lists", "shopping_lists", None),
    ("shopping_list_items", "shopping_list", None),
    ("recipes", "recipes", "type=normal"),
    ("meal_plan_entries", "meal_plan", None),
    ("chores", "chores", None),
    ("open_tasks", "tasks", "done=0"),
    ("batteries", "batteries", None),
    ("equipment", "equipment", None),
]


# -----------------------------------------------------------------------------
# helpers
# -----------------------------------------------------------------------------


def in_addon() -> bool:
    return bool(os.getenv("SUPERVISOR_TOKEN")) or Path("/data/options.json").exists()


def _settings_path() -> Path:
    if os.getenv("MCP_UI_SETTINGS"):
        return Path(os.environ["MCP_UI_SETTINGS"])
    if Path("/data").is_dir() and os.access("/data", os.W_OK):
        return Path("/data/ui_settings.json")
    return Path.home() / ".grocy-mcp" / "ui_settings.json"


def _load_settings() -> dict:
    try:
        return json.loads(_settings_path().read_text())
    except Exception:
        return {}


def _save_settings(data: dict) -> None:
    path = _settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _category(name: str) -> tuple[str, str | None]:
    for prefix, cat, feature in CATEGORY_RULES:
        if name.startswith(prefix):
            return cat, feature
    return "Other", None


def _allowed(request: Request) -> bool:
    if os.getenv("MCP_UI_ENABLED", "1") == "0":
        return False
    peer = request.client.host if request.client else ""
    if in_addon():
        return peer == SUPERVISOR_INGRESS_IP
    if os.getenv("MCP_UI_ALLOW_REMOTE") == "1":
        return True
    return peer in ("127.0.0.1", "::1", "localhost")


def _guard(handler):
    async def wrapped(request: Request) -> Response:
        if not _allowed(request):
            return JSONResponse(
                {
                    "error": "The web UI is only available through Home Assistant (Open Web UI) "
                    "or from this machine. Set MCP_UI_ALLOW_REMOTE=1 to change that."
                },
                status_code=403,
            )
        try:
            return await handler(request)
        except Exception as e:  # never leak a stack trace into the UI
            log.exception("UI handler failed")
            return JSONResponse({"error": str(e).splitlines()[0][:300]}, status_code=500)

    wrapped.__name__ = handler.__name__
    return wrapped


def _client():
    from grocy_mcp.client import GrocyClient
    from grocy_mcp.config import load_config

    config = load_config()  # env is read at call time
    return GrocyClient(config.url, config.api_key)


async def _get(client, path: str, params: dict | None = None) -> Any:
    resp = await client._request("GET", path, params=params)
    return resp.json()


async def _count(client, entity: str, query: str | None) -> int | None:
    try:
        return len(await client.get_objects(entity, query))
    except Exception:
        return None


def _features(config: dict) -> dict[str, bool]:
    out = {}
    for f in FEATURES:
        value = config.get(f"FEATURE_FLAG_{f}", True)
        out[f] = value not in (False, 0, "0", "false")
    return out


def _server_version() -> str:
    from grocy_mcp import __version__

    return __version__


def _snapshot_tools(mcp) -> dict[str, dict]:
    """Capture every registered tool once, before any are disabled."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        tools = asyncio.run(mcp.list_tools())
    else:  # called from inside an event loop (tests, embedding): use a worker thread
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            tools = pool.submit(asyncio.run, mcp.list_tools()).result()
    out = {}
    for t in tools:
        desc = (t.description or "").strip().split("\n\n")[0].replace("\n", " ")
        cat, feature = _category(t.name)
        out[t.name] = {
            "name": t.name,
            "category": cat,
            "feature": feature,
            "description": desc[:300],
        }
    return out


def _parse_dt(value: Any) -> datetime | None:
    from grocy_mcp.core.utils import parse_datetime

    return parse_datetime(value) if value else None


def _product_rows(entries: list[dict], date_key: str = "best_before_date") -> list[dict]:
    rows = []
    for e in entries or []:
        product = e.get("product") or {}
        rows.append(
            {
                "name": product.get("name") or e.get("name") or f"Product {e.get('product_id')}",
                "amount": e.get("amount"),
                "date": e.get(date_key),
            }
        )
    rows.sort(key=lambda r: str(r["date"] or ""))
    return rows


async def _attention(client, features: dict[str, bool], due_days: int) -> dict:
    now = datetime.now(tz=timezone.utc)
    today = date.today().isoformat()
    out: dict[str, Any] = {"due_soon_days": due_days}

    if features.get("STOCK"):
        vol = await _get(client, "/stock/volatile", {"due_soon_days": due_days})
        out["expired"] = _product_rows(vol.get("expired_products"))
        out["overdue"] = _product_rows(vol.get("overdue_products"))
        out["due_soon"] = _product_rows(vol.get("due_products"))
        out["below_min_stock"] = sorted(
            (
                {
                    "name": m.get("name") or f"Product {m.get('id')}",
                    "missing": m.get("amount_missing"),
                    "partly_in_stock": bool(int(m.get("is_partly_in_stock") or 0)),
                }
                for m in vol.get("missing_products") or []
            ),
            key=lambda r: r["name"].lower(),
        )

    if features.get("CHORES"):
        rows = []
        for c in await client.get_chores():
            nxt = _parse_dt(c.get("next_estimated_execution_time"))
            if nxt and nxt < now:
                name = (c.get("chore") or {}).get("name") or c.get("chore_name")
                rows.append(
                    {
                        "name": name or f"Chore {c.get('chore_id')}",
                        "date": c.get("next_estimated_execution_time"),
                    }
                )
        out["overdue_chores"] = sorted(rows, key=lambda r: str(r["date"]))

    if features.get("TASKS"):
        rows = [
            {"name": t.get("name") or f"Task {t.get('id')}", "date": t.get("due_date")}
            for t in await client.get_tasks()
            if not int(t.get("done") or 0) and t.get("due_date") and t["due_date"][:10] < today
        ]
        out["overdue_tasks"] = sorted(rows, key=lambda r: str(r["date"]))

    if features.get("BATTERIES"):
        from grocy_mcp.core.batteries import batteries_overdue_data

        out["overdue_batteries"] = [
            {
                "name": (b.get("battery") or {}).get("name")
                or b.get("name")
                or f"Battery {b.get('battery_id') or b.get('id')}",
                "date": b.get("next_estimated_charge_time"),
            }
            for b in await batteries_overdue_data(client)
        ]
    return out


# -----------------------------------------------------------------------------
# registration
# -----------------------------------------------------------------------------


def register_ui(mcp) -> None:
    """Attach the web UI routes to the FastMCP server and apply saved tool settings."""
    if os.getenv("MCP_UI_ENABLED", "1") == "0":
        log.info("Web UI disabled (MCP_UI_ENABLED=0)")
        return
    if not hasattr(mcp, "custom_route"):
        log.warning("This fastmcp version has no custom_route; web UI not available")
        return

    _STATE["tools"] = _snapshot_tools(mcp)
    saved = _load_settings()
    disabled = {n for n in saved.get("disabled_tools", []) if n in _STATE["tools"]}
    if disabled and hasattr(mcp, "disable"):
        mcp.disable(names=disabled)
    _STATE["disabled"] = disabled
    server_version = _server_version()

    @mcp.custom_route("/", methods=["GET"])
    @_guard
    async def ui_index(request: Request) -> Response:
        return HTMLResponse(PAGE.replace("{{VERSION}}", server_version))

    @mcp.custom_route("/ui/api/status", methods=["GET"])
    @_guard
    async def ui_status(request: Request) -> Response:
        grocy: dict[str, Any] = {"url": os.getenv("GROCY_URL", ""), "connected": False}
        try:
            async with _client() as c:
                info = await c.get_system_info()
                grocy.update(
                    {
                        "connected": True,
                        "version": (info.get("grocy_version") or {}).get("Version"),
                    }
                )
                try:
                    grocy["features"] = _features(await _get(c, "/system/config"))
                except Exception:
                    pass
                try:
                    me = await _get(c, "/user")
                    if isinstance(me, list):
                        me = me[0] if me else {}
                    grocy["user"] = me.get("display_name") or me.get("username")
                except Exception:
                    pass
        except Exception as e:
            grocy["error"] = str(e).splitlines()[0][:300]
        all_tools = _STATE["tools"]
        return JSONResponse(
            {
                "server": {
                    "version": server_version,
                    "uptime_seconds": int(time.time() - _STARTED),
                    "port": int(os.getenv("MCP_PORT", "8100")),
                    "path": os.getenv("MCP_PATH", "/mcp"),
                    "auth_enabled": bool(os.getenv("MCP_AUTH_TOKEN", "").strip()),
                    "in_home_assistant": in_addon(),
                    "hostname": os.getenv("HOSTNAME", ""),
                },
                "grocy": grocy,
                "tools": {"total": len(all_tools), "disabled": len(_STATE["disabled"])},
            }
        )

    @mcp.custom_route("/ui/api/tools", methods=["GET"])
    @_guard
    async def ui_tools(request: Request) -> Response:
        rows = [
            {**t, "enabled": t["name"] not in _STATE["disabled"]} for t in _STATE["tools"].values()
        ]
        rows.sort(key=lambda r: (r["category"], r["name"]))
        return JSONResponse({"tools": rows})

    @mcp.custom_route("/ui/api/tools", methods=["POST"])
    @_guard
    async def ui_tools_update(request: Request) -> Response:
        body = await request.json()
        names = set(_STATE["tools"])
        changes = body.get("changes") or [
            {"name": body.get("name"), "enabled": body.get("enabled")}
        ]
        for ch in changes:
            name, enabled = ch.get("name"), bool(ch.get("enabled"))
            if name not in names:
                return JSONResponse({"error": f"unknown tool {name}"}, status_code=400)
            if enabled:
                _STATE["disabled"].discard(name)
                mcp.enable(names={name})
            else:
                _STATE["disabled"].add(name)
                mcp.disable(names={name})
        settings = _load_settings()
        settings["disabled_tools"] = sorted(_STATE["disabled"])
        _save_settings(settings)
        return JSONResponse(
            {
                "ok": True,
                "disabled": sorted(_STATE["disabled"]),
                "note": "Clients see the change after they refresh their tool list.",
            }
        )

    @mcp.custom_route("/ui/api/library", methods=["GET"])
    @_guard
    async def ui_library(request: Request) -> Response:
        async with _client() as c:
            values = await asyncio.gather(
                *(_count(c, entity, query) for _, entity, query in LIBRARY_ENTITIES)
            )
        return JSONResponse({"counts": dict(zip((k for k, _, _ in LIBRARY_ENTITIES), values))})

    @mcp.custom_route("/ui/api/attention", methods=["GET"])
    @_guard
    async def ui_attention(request: Request) -> Response:
        try:
            due_days = max(0, min(int(request.query_params.get("days", "5")), 365))
        except ValueError:
            due_days = 5
        async with _client() as c:
            try:
                features = _features(await _get(c, "/system/config"))
            except Exception:
                features = {f: True for f in FEATURES}
            return JSONResponse(await _attention(c, features, due_days))

    log.info("Web UI enabled (%s)", "Home Assistant ingress only" if in_addon() else "local access")


# -----------------------------------------------------------------------------
# page
# -----------------------------------------------------------------------------

PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grocy MCP</title>
<link rel="icon" href="data:,">
<style>
:root{--bg:#f5f6f8;--card:#fff;--text:#1d2329;--muted:#5f6b76;--line:#e3e7eb;--accent:#0f7b6c;--accent-ink:#fff;--ok:#2e7d32;--bad:#c62828;--warn:#b26a00;--code:#f0f2f4}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--card:#1a1f25;--text:#e6e9ec;--muted:#9aa5b1;--line:#2a3139;--code:#232a32;--accent:#1a9c8a}}
*{box-sizing:border-box}body{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--text)}
header{display:flex;align-items:center;gap:12px;padding:16px 24px;background:var(--card);border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0}header .ver{color:var(--muted);font-size:13px}
.logo{width:30px;height:30px;border-radius:8px;background:var(--accent);display:grid;place-items:center;color:var(--accent-ink);font-weight:700}
nav{display:flex;gap:4px;padding:0 24px;background:var(--card);border-bottom:1px solid var(--line);overflow-x:auto}
nav button{border:0;background:none;color:var(--muted);padding:12px 14px;font:inherit;cursor:pointer;border-bottom:2px solid transparent}
nav button.active{color:var(--text);border-bottom-color:var(--accent)}
main{max-width:1100px;margin:0 auto;padding:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px}
.card h2{font-size:15px;margin:0 0 10px}.muted{color:var(--muted)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;font-size:14px}.kv dt{color:var(--muted)}.kv dd{margin:0;word-break:break-all}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600}
.pill.ok{background:rgba(46,125,50,.14);color:var(--ok)}.pill.bad{background:rgba(198,40,40,.14);color:var(--bad)}.pill.warn{background:rgba(178,106,0,.15);color:var(--warn)}.pill.off{background:var(--code);color:var(--muted)}
.stat{font-size:28px;font-weight:700}.stat small{font-size:13px;font-weight:400;color:var(--muted);display:block}
button.btn{background:var(--accent);color:var(--accent-ink);border:0;border-radius:8px;padding:8px 14px;font:inherit;cursor:pointer}
button.btn.secondary{background:transparent;color:var(--text);border:1px solid var(--line)}button.btn:disabled{opacity:.6;cursor:default}
pre{background:var(--code);padding:12px;border-radius:8px;overflow:auto;font-size:13px;margin:8px 0 0;white-space:pre-wrap;word-break:break-all}
.snip{position:relative}.snip button{position:absolute;top:6px;right:6px;font-size:12px;padding:4px 8px}
input[type=text],input[type=search],input[type=number]{width:100%;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--text);font:inherit}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-weight:600}
.cat{margin-top:22px}.cat h3{font-size:14px;margin:0 0 6px;display:flex;justify-content:space-between;align-items:center;gap:8px}
.switch{position:relative;width:38px;height:22px;flex:none}.switch input{opacity:0;width:0;height:0}
.slider{position:absolute;inset:0;background:var(--line);border-radius:22px;cursor:pointer;transition:.15s}.slider:before{content:"";position:absolute;width:16px;height:16px;left:3px;top:3px;background:#fff;border-radius:50%;transition:.15s}
.switch input:checked+.slider{background:var(--accent)}.switch input:checked+.slider:before{transform:translateX(16px)}
.tool{display:flex;gap:12px;align-items:flex-start;padding:9px 0;border-bottom:1px solid var(--line)}.tool code{font-size:13px}.tool p{margin:2px 0 0;font-size:13px;color:var(--muted)}
.toast{position:fixed;bottom:18px;right:18px;background:var(--text);color:var(--bg);padding:10px 14px;border-radius:8px;opacity:0;transition:.2s}.toast.show{opacity:1}
.hidden{display:none}.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.feat{display:flex;flex-wrap:wrap;gap:6px}
</style>
</head>
<body>
<header><div class="logo">G</div><div><h1>Grocy MCP Server</h1><div class="ver">v{{VERSION}}</div></div></header>
<nav id="tabs">
  <button data-tab="overview" class="active">Overview</button>
  <button data-tab="connect">Connect</button>
  <button data-tab="tools">Tools</button>
  <button data-tab="library">Library</button>
  <button data-tab="attention">Attention</button>
</nav>
<main>
<section id="overview">
  <div class="grid">
    <div class="card"><h2>Grocy</h2><dl class="kv" id="grocy-kv"><dt>Status</dt><dd>Checking…</dd></dl></div>
    <div class="card"><h2>MCP server</h2><dl class="kv" id="server-kv"></dl></div>
    <div class="card"><h2>Tools</h2><div class="stat" id="tool-stat">–</div><p class="muted" id="tool-note"></p></div>
  </div>
  <div class="card" style="margin-top:16px"><h2>Grocy features</h2><div class="feat" id="features"><span class="muted">–</span></div><p class="muted" style="margin:8px 0 0">Features are switched on and off in Grocy itself (for the Community Grocy app: its Configuration tab → features). Tools for a feature that is off return errors.</p></div>
  <div class="card" style="margin-top:16px" id="warnings"></div>
  <div class="row" style="margin-top:16px"><button class="btn secondary" onclick="loadStatus()">Refresh</button></div>
</section>

<section id="connect" class="hidden">
  <div class="card">
    <h2>Server address</h2>
    <p class="muted">How your MCP client reaches this server. On your LAN this is usually your Home Assistant host's IP with port <span id="port-hint">8100</span>. For claude.ai custom connectors, use a public HTTPS URL (for example a Cloudflare tunnel) that points at this port.</p>
    <input type="text" id="mcp-url">
    <p class="muted" style="margin-top:8px">Bearer token: <span id="auth-hint"></span></p>
  </div>
  <div class="grid" style="margin-top:16px">
    <div class="card"><h2>Claude Code</h2><div class="snip"><button class="btn secondary" onclick="copy('snip-cc')">Copy</button><pre id="snip-cc"></pre></div></div>
    <div class="card"><h2>Claude Desktop (claude_desktop_config.json)</h2><div class="snip"><button class="btn secondary" onclick="copy('snip-cd')">Copy</button><pre id="snip-cd"></pre></div></div>
    <div class="card"><h2>claude.ai custom connector / other clients</h2><div class="snip"><button class="btn secondary" onclick="copy('snip-gen')">Copy</button><pre id="snip-gen"></pre></div></div>
  </div>
</section>

<section id="tools" class="hidden">
  <div class="card">
    <div class="row"><input type="search" id="tool-search" placeholder="Search tools…" style="flex:1"><span class="muted" id="tool-count"></span></div>
    <p class="muted" style="margin:8px 0 0">Turned-off tools are hidden from MCP clients. Clients pick up changes when they refresh their tool list or reconnect. Settings survive restarts and updates.</p>
    <div class="row hidden" id="feature-off-row" style="margin-top:10px"><span class="muted" id="feature-off-note" style="flex:1"></span><button class="btn secondary" onclick="disableOffFeatures()">Turn those off</button></div>
    <div id="tool-list"></div>
  </div>
</section>

<section id="library" class="hidden">
  <div class="grid" id="lib-grid"></div>
  <div class="row" style="margin-top:16px"><button class="btn secondary" onclick="loadLibrary()">Refresh</button></div>
</section>

<section id="attention" class="hidden">
  <div class="card">
    <div class="row"><h2 style="margin:0;flex:1">Needs attention</h2><label class="muted">Due within <input type="number" id="due-days" value="5" min="0" max="365" style="width:70px;display:inline-block"> days</label><button class="btn" id="att-btn" onclick="loadAttention()">Check</button></div>
    <p class="muted">Expired, overdue and soon-due stock, products below their minimum stock amount, and overdue chores, tasks and batteries. Sections for Grocy features that are off are skipped.</p>
    <div id="att-out"></div>
  </div>
</section>
</main>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.querySelector(s);let STATUS=null,TOOLS=[];
const FEAT={STOCK:'Stock',SHOPPINGLIST:'Shopping lists',RECIPES:'Recipes & meal plan',CHORES:'Chores',TASKS:'Tasks',BATTERIES:'Batteries',EQUIPMENT:'Equipment',CALENDAR:'Calendar'};
function toast(m){const t=$('#toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),2500)}
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function api(p,o){const r=await fetch('ui/api/'+p,o);const j=await r.json().catch(()=>({error:'Bad response'}));if(!r.ok||j.error)throw new Error(j.error||r.statusText);return j}
function copy(id){navigator.clipboard.writeText($('#'+id).textContent).then(()=>toast('Copied'))}
document.querySelectorAll('#tabs button').forEach(b=>b.onclick=()=>{document.querySelectorAll('#tabs button').forEach(x=>x.classList.toggle('active',x===b));document.querySelectorAll('main>section').forEach(s=>s.classList.toggle('hidden',s.id!==b.dataset.tab));({tools:loadTools,library:loadLibrary,attention:loadAttention}[b.dataset.tab]||(()=>{}))()});
function fmtUp(s){const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);return (d?d+'d ':'')+(h?h+'h ':'')+m+'m'}
function featureOff(f){return !!(f&&STATUS&&STATUS.grocy.features&&STATUS.grocy.features[f]===false)}
async function loadStatus(){
  try{STATUS=await api('status')}catch(e){$('#grocy-kv').innerHTML='<dt>Error</dt><dd>'+esc(e.message)+'</dd>';return}
  const g=STATUS.grocy,s=STATUS.server;
  $('#grocy-kv').innerHTML=`<dt>Status</dt><dd>${g.connected?'<span class="pill ok">Connected</span>':'<span class="pill bad">Not connected</span>'}</dd>
   <dt>URL</dt><dd>${esc(g.url)}</dd>${g.version?`<dt>Version</dt><dd>${esc(g.version)}</dd>`:''}
   ${g.user?`<dt>Key user</dt><dd>${esc(g.user)}</dd>`:''}
   ${g.error?`<dt>Error</dt><dd>${esc(g.error)}</dd>`:''}`;
  $('#server-kv').innerHTML=`<dt>Version</dt><dd>${esc(s.version)}</dd><dt>Uptime</dt><dd>${fmtUp(s.uptime_seconds)}</dd>
   <dt>Endpoint</dt><dd>:${s.port}${esc(s.path)}</dd><dt>Auth</dt><dd>${s.auth_enabled?'<span class="pill ok">Bearer token</span>':'<span class="pill bad">None</span>'}</dd>`;
  $('#tool-stat').innerHTML=`${STATUS.tools.total-STATUS.tools.disabled}<small>of ${STATUS.tools.total} tools enabled</small>`;
  const f=g.features;$('#features').innerHTML=f?Object.entries(FEAT).map(([k,l])=>`<span class="pill ${f[k]===false?'off':'ok'}">${esc(l)}${f[k]===false?' · off':''}</span>`).join(''):'<span class="muted">Unknown until Grocy is reachable.</span>';
  const w=[];if(!s.auth_enabled)w.push('No MCP bearer token is set, so anyone who can reach port '+s.port+' can use this server. Set one on the app\'s Configuration tab.');
  if(!g.connected)w.push('The server cannot reach Grocy. Check the Grocy URL and API key on the Configuration tab.');
  if(f){const off=Object.keys(FEAT).filter(k=>f[k]===false);if(off.length){await ensureTools();const n=TOOLS.filter(t=>t.enabled&&featureOff(t.feature)).length;if(n)w.push(`${n} enabled tools belong to Grocy features that are off (${off.map(k=>FEAT[k]).join(', ')}). They will return errors; you can turn them off on the Tools tab.`)}}
  $('#warnings').innerHTML=w.length?'<h2>Attention</h2>'+w.map(x=>'<p>⚠️ '+esc(x)+'</p>').join(''):'<h2>All good</h2><p class="muted">No problems found.</p>';
  $('#port-hint').textContent=s.port;
  const host=(location.hostname&&!location.hostname.includes('hassio'))?location.hostname:'homeassistant.local';
  if(!$('#mcp-url').value)$('#mcp-url').value=`http://${host}:${s.port}${s.path}`;
  $('#auth-hint').innerHTML=s.auth_enabled?'set. Use the value from the app\'s Configuration tab where the snippets say <code>YOUR_TOKEN</code>.':'<span class="pill bad">not set</span>, so the snippets omit it.';
  renderSnippets();
}
function renderSnippets(){
  if(!STATUS)return;const url=$('#mcp-url').value.trim(),auth=STATUS.server.auth_enabled,http=url.startsWith('http:');
  $('#snip-cc').textContent=`claude mcp add --transport http grocy ${url}`+(auth?` \\\n  --header "Authorization: Bearer YOUR_TOKEN"`:'');
  const args=['-y','mcp-remote',url];if(http)args.push('--allow-http');const srv={command:'npx',args};
  if(auth){args.push('--header','Authorization:${GROCY_MCP_AUTH}');srv.env={GROCY_MCP_AUTH:'Bearer YOUR_TOKEN'}}
  $('#snip-cd').textContent=JSON.stringify({mcpServers:{grocy:srv}},null,2);
  $('#snip-gen').textContent=`URL:       ${url}\nTransport: Streamable HTTP`+(auth?`\nHeader:    Authorization: Bearer YOUR_TOKEN`:'');
}
$('#mcp-url').addEventListener('input',renderSnippets);
async function ensureTools(){if(!TOOLS.length){try{TOOLS=(await api('tools')).tools}catch(e){}}}
async function loadTools(){
  try{TOOLS=(await api('tools')).tools}catch(e){$('#tool-list').innerHTML='<p>'+esc(e.message)+'</p>';return}renderTools()}
function renderTools(){
  const q=$('#tool-search').value.toLowerCase();const groups={};
  TOOLS.filter(t=>!q||t.name.includes(q)||t.description.toLowerCase().includes(q)).forEach(t=>(groups[t.category]=groups[t.category]||[]).push(t));
  $('#tool-count').textContent=`${TOOLS.filter(t=>t.enabled).length} / ${TOOLS.length} enabled`;
  const offOn=TOOLS.filter(t=>t.enabled&&featureOff(t.feature));
  $('#feature-off-row').classList.toggle('hidden',!offOn.length);
  $('#feature-off-note').textContent=`${offOn.length} enabled tools belong to Grocy features that are off and will return errors.`;
  $('#tool-list').innerHTML=Object.entries(groups).map(([cat,ts])=>`<div class="cat"><h3><span>${esc(cat)} ${featureOff(ts[0].feature)?'<span class="pill off">off in Grocy</span>':''}</span><span class="row"><button class="btn secondary" onclick="setGroup('${esc(cat)}',true)">All on</button><button class="btn secondary" onclick="setGroup('${esc(cat)}',false)">All off</button></span></h3>`+
   ts.map(t=>`<div class="tool"><label class="switch"><input type="checkbox" ${t.enabled?'checked':''} onchange="setTool('${t.name}',this.checked)"><span class="slider"></span></label><div><code>${esc(t.name)}</code><p>${esc(t.description)}</p></div></div>`).join('')+'</div>').join('');
}
$('#tool-search').addEventListener('input',renderTools);
async function save(changes){if(!changes.length)return;try{await api('tools',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({changes})});changes.forEach(c=>{const t=TOOLS.find(x=>x.name===c.name);if(t)t.enabled=c.enabled});renderTools();toast('Saved')}catch(e){toast(e.message);loadTools()}}
function setTool(n,on){save([{name:n,enabled:on}])}
function setGroup(cat,on){save(TOOLS.filter(t=>t.category===cat).map(t=>({name:t.name,enabled:on})))}
function disableOffFeatures(){save(TOOLS.filter(t=>t.enabled&&featureOff(t.feature)).map(t=>({name:t.name,enabled:false})))}
const LIB=[['products','Products'],['locations','Locations'],['product_groups','Product groups'],['quantity_units','Quantity units'],['barcodes','Barcodes'],['shopping_lists','Shopping lists'],['shopping_list_items','Shopping list items'],['recipes','Recipes'],['meal_plan_entries','Meal plan entries'],['chores','Chores'],['open_tasks','Open tasks'],['batteries','Batteries'],['equipment','Equipment']];
async function loadLibrary(){$('#lib-grid').innerHTML='<div class="card">Loading…</div>';try{const c=(await api('library')).counts;$('#lib-grid').innerHTML=LIB.map(([k,l])=>`<div class="card"><div class="stat">${c[k]??'–'}<small>${l}</small></div></div>`).join('')}catch(e){$('#lib-grid').innerHTML='<div class="card">'+esc(e.message)+'</div>'}}
const ATT=[['expired','Expired','bad'],['overdue','Past best-before','warn'],['due_soon','Due soon','warn'],['below_min_stock','Below minimum stock','warn'],['overdue_chores','Overdue chores','bad'],['overdue_tasks','Overdue tasks','bad'],['overdue_batteries','Batteries due for a charge','warn']];
function fmtDate(d){return esc(String(d||'').replace('T',' ').slice(0,16))}
async function loadAttention(){const b=$('#att-btn');b.disabled=true;b.textContent='Checking…';
  try{const r=await api('attention?days='+encodeURIComponent($('#due-days').value||5));const present=ATT.filter(([k])=>k in r);
   let h='<div class="grid" style="margin-top:12px">'+present.map(([k,l,c])=>`<div class="card"><div class="stat">${r[k].length}<small>${l}</small></div></div>`).join('')+'</div>';
   present.filter(([k])=>r[k].length).forEach(([k,l,c])=>{
     const rows=r[k];let head,body;
     if(k==='below_min_stock'){head='<tr><th>Product</th><th>Missing</th><th></th></tr>';body=rows.map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.missing)}</td><td>${x.partly_in_stock?'<span class="pill warn">partly in stock</span>':''}</td></tr>`).join('')}
     else if('amount' in rows[0]){head='<tr><th>Product</th><th>Amount</th><th>Date</th></tr>';body=rows.map(x=>`<tr><td>${esc(x.name)}</td><td>${esc(x.amount)}</td><td>${fmtDate(x.date)}</td></tr>`).join('')}
     else{head='<tr><th>Name</th><th>Was due</th></tr>';body=rows.map(x=>`<tr><td>${esc(x.name)}</td><td>${fmtDate(x.date)}</td></tr>`).join('')}
     h+=`<h3 style="margin-top:18px"><span class="pill ${c}">${rows.length}</span> ${l}</h3><table>${head}${body}</table>`});
   if(present.every(([k])=>!r[k].length))h+='<p class="muted" style="margin-top:12px">Nothing needs attention.</p>';
   $('#att-out').innerHTML=h}catch(e){$('#att-out').textContent=e.message}b.disabled=false;b.textContent='Check'}
loadStatus();
</script>
</body>
</html>
"""
