"""Tests for the web UI (Home Assistant ingress panel)."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from grocy_mcp.mcp import web_ui
from grocy_mcp.mcp.http_transport import build_http_app
from grocy_mcp.mcp.server import create_mcp_server

GROCY = "http://grocy.test"
API = f"{GROCY}/api"


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("GROCY_URL", GROCY)
    monkeypatch.setenv("GROCY_API_KEY", "key")
    monkeypatch.setenv("MCP_UI_SETTINGS", str(tmp_path / "ui_settings.json"))
    monkeypatch.delenv("SUPERVISOR_TOKEN", raising=False)
    monkeypatch.delenv("MCP_UI_ALLOW_REMOTE", raising=False)
    monkeypatch.setattr(web_ui, "in_addon", lambda: False)
    return tmp_path


def _app(token="s3cret"):
    mcp = create_mcp_server()
    web_ui.register_ui(mcp)
    return mcp, build_http_app(mcp, token=token)


async def _call(app, method, path, peer="127.0.0.1", **kw):
    transport = httpx.ASGITransport(app=app, client=(peer, 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://ui") as c:
        return await c.request(method, path, **kw)


def _mock_grocy(features=None):
    flags = {f"FEATURE_FLAG_{k}": True for k in web_ui.FEATURES}
    flags.update({f"FEATURE_FLAG_{k}": v for k, v in (features or {}).items()})
    respx.get(f"{API}/system/info").respond(json={"grocy_version": {"Version": "4.5.0"}})
    respx.get(f"{API}/system/config").respond(json=flags)
    respx.get(f"{API}/user").respond(json={"username": "admin", "display_name": "Andrew"})


async def test_index_served_without_bearer(env):
    _, app = _app()
    resp = await _call(app, "GET", "/")
    assert resp.status_code == 200
    assert "Grocy MCP Server" in resp.text


async def test_mcp_endpoint_still_requires_bearer(env):
    _, app = _app()
    resp = await _call(app, "POST", "/mcp", json={})
    assert resp.status_code == 401


async def test_remote_peer_forbidden(env):
    _, app = _app()
    resp = await _call(app, "GET", "/ui/api/status", peer="192.168.1.20")
    assert resp.status_code == 403


async def test_addon_only_answers_supervisor(env, monkeypatch):
    _, app = _app()
    monkeypatch.setattr(web_ui, "in_addon", lambda: True)
    assert (await _call(app, "GET", "/", peer="127.0.0.1")).status_code == 403
    assert (await _call(app, "GET", "/", peer=web_ui.SUPERVISOR_INGRESS_IP)).status_code == 200


@respx.mock
async def test_status_reports_grocy_and_features(env):
    _mock_grocy({"RECIPES": False})
    _, app = _app()
    data = (await _call(app, "GET", "/ui/api/status")).json()
    assert data["grocy"]["connected"] is True
    assert data["grocy"]["version"] == "4.5.0"
    assert data["grocy"]["user"] == "Andrew"
    assert data["grocy"]["features"]["RECIPES"] is False
    assert data["grocy"]["features"]["STOCK"] is True
    assert data["server"]["auth_enabled"] is False  # token passed to the app, not the env
    assert data["tools"]["total"] > 50


@respx.mock
async def test_status_when_grocy_unreachable(env):
    respx.get(f"{API}/system/info").mock(side_effect=httpx.ConnectError("refused"))
    _, app = _app()
    data = (await _call(app, "GET", "/ui/api/status")).json()
    assert data["grocy"]["connected"] is False
    assert "refused" in data["grocy"]["error"]


async def test_tools_have_categories_and_features(env):
    _, app = _app()
    tools = {t["name"]: t for t in (await _call(app, "GET", "/ui/api/tools")).json()["tools"]}
    assert tools["recipe_details_tool"]["feature"] == "RECIPES"
    assert tools["meal_plan_list_tool"]["feature"] == "RECIPES"
    assert tools["print_battery_label_tool"]["category"] == "Batteries"
    assert tools["workflow_shopping_reconcile_apply_tool"]["category"] == "Shopping lists"
    assert tools["stock_add_tool"]["category"] == "Stock"
    assert all(t["category"] != "Other" for t in tools.values())


async def test_tool_toggle_hides_tool_and_persists(env):
    mcp, app = _app()
    resp = await _call(
        app,
        "POST",
        "/ui/api/tools",
        json={"changes": [{"name": "recipe_details_tool", "enabled": False}]},
    )
    assert resp.json()["disabled"] == ["recipe_details_tool"]
    assert "recipe_details_tool" not in {t.name for t in await mcp.list_tools()}
    saved = json.loads((env / "ui_settings.json").read_text())
    assert saved["disabled_tools"] == ["recipe_details_tool"]

    # A fresh server (app restart) applies the saved setting.
    mcp2, _ = _app()
    assert "recipe_details_tool" not in {t.name for t in await mcp2.list_tools()}

    await _call(app, "POST", "/ui/api/tools", json={"name": "recipe_details_tool", "enabled": True})
    assert "recipe_details_tool" in {t.name for t in await mcp.list_tools()}


async def test_tool_toggle_rejects_unknown(env):
    _, app = _app()
    resp = await _call(app, "POST", "/ui/api/tools", json={"name": "nope", "enabled": False})
    assert resp.status_code == 400


@respx.mock
async def test_library_counts(env):
    respx.get(f"{API}/objects/products").respond(json=[{"id": 1}, {"id": 2}])
    respx.get(f"{API}/objects/recipes").respond(json=[{"id": 1}])
    respx.get(url__regex=rf"{API}/objects/.*").respond(json=[])
    _, app = _app()
    counts = (await _call(app, "GET", "/ui/api/library")).json()["counts"]
    assert counts["products"] == 2
    assert counts["recipes"] == 1
    assert counts["chores"] == 0


@respx.mock
async def test_attention(env):
    _mock_grocy({"BATTERIES": False})
    respx.get(f"{API}/stock/volatile").respond(
        json={
            "expired_products": [
                {
                    "product_id": 1,
                    "amount": 2,
                    "best_before_date": "2026-01-01",
                    "product": {"name": "Milk"},
                }
            ],
            "overdue_products": [],
            "due_products": [
                {
                    "product_id": 3,
                    "amount": 1,
                    "best_before_date": "2099-01-01",
                    "product": {"name": "Eggs"},
                }
            ],
            "missing_products": [
                {"id": 4, "name": "Coffee", "amount_missing": 1, "is_partly_in_stock": 0}
            ],
        }
    )
    respx.get(f"{API}/chores").respond(
        json=[
            {
                "chore_id": 1,
                "chore_name": "Vacuum",
                "next_estimated_execution_time": "2020-01-01 10:00:00",
            },
            {
                "chore_id": 2,
                "chore_name": "Later",
                "next_estimated_execution_time": "2999-01-01 10:00:00",
            },
        ]
    )
    respx.get(f"{API}/tasks").respond(
        json=[
            {"id": 1, "name": "Old task", "done": 0, "due_date": "2020-01-01"},
            {"id": 2, "name": "Done task", "done": 1, "due_date": "2020-01-01"},
            {"id": 3, "name": "No date", "done": 0, "due_date": None},
        ]
    )
    _, app = _app()
    resp = await _call(app, "GET", "/ui/api/attention?days=3")
    data = resp.json()
    assert data["due_soon_days"] == 3
    volatile = [c for c in respx.calls if c.request.url.path.endswith("/stock/volatile")]
    assert volatile[0].request.url.params["due_soon_days"] == "3"
    assert [r["name"] for r in data["expired"]] == ["Milk"]
    assert [r["name"] for r in data["due_soon"]] == ["Eggs"]
    assert data["below_min_stock"] == [{"name": "Coffee", "missing": 1, "partly_in_stock": False}]
    assert [r["name"] for r in data["overdue_chores"]] == ["Vacuum"]
    assert [r["name"] for r in data["overdue_tasks"]] == ["Old task"]
    assert "overdue_batteries" not in data  # feature off in Grocy


async def test_ui_disabled(env, monkeypatch):
    monkeypatch.setenv("MCP_UI_ENABLED", "0")
    _, app = _app()
    assert (await _call(app, "GET", "/")).status_code == 404
