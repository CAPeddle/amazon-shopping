"""The served ASGI app: health routes, the bearer gate, and MCP over HTTP.

The last test is the one that matters most — it drives a real
``streamable_http_client`` through the mounted app, so a mounting or lifespan
mistake fails here rather than the first time a client connects.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import httpx2
import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from amazon_nl_mcp.app import build_app
from amazon_nl_mcp.config import Settings

# Must be an allowed Host: MCP's DNS-rebinding protection rejects "testserver".
BASE_URL = "http://127.0.0.1:8765"


@pytest.fixture
def http_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "host": "127.0.0.1",
            "port": 8765,
            # Point the browser at nothing, so these tests exercise the degraded
            # path — service up, Chromium down — whatever the machine has installed.
            "browser_executable_path": Path("/nonexistent/chrome"),
        }
    )


@pytest.fixture
async def app(http_settings: Settings) -> AsyncIterator[FastAPI]:
    # ASGITransport does not run lifespans, and the lifespan is where the MCP
    # session manager is started; without it the first request would fail with
    # "Task group is not initialized".
    built = build_app(http_settings)
    async with LifespanManager(built):
        yield built


@pytest.fixture
async def anon(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:
        yield client


async def test_healthz_is_public_and_says_nothing_about_the_account(anon: httpx.AsyncClient) -> None:
    response = await anon.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert set(body) == {"status", "browser_running", "restarts", "write_enabled"}


async def test_root_points_a_client_at_the_endpoint(anon: httpx.AsyncClient) -> None:
    body = (await anon.get("/")).json()
    assert body["mcp_endpoint"] == "/mcp"
    assert body["transport"] == "streamable-http"
    assert body["auth"] == "bearer"


async def test_the_mcp_endpoint_refuses_anonymous_callers(anon: httpx.AsyncClient) -> None:
    response = await anon.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    assert "AMAZON_MCP_AUTH_TOKEN" in response.json()["detail"]


@pytest.mark.parametrize(
    "header",
    ["", "Bearer", "Bearer ", "Basic abc", "Bearer wrong-token-wrong-token", "bearer "],
)
async def test_malformed_and_wrong_tokens_are_all_rejected(anon: httpx.AsyncClient, header: str) -> None:
    response = await anon.post("/mcp", json={}, headers={"Authorization": header})
    assert response.status_code == 401


async def test_readyz_reports_not_ready_without_a_browser(anon: httpx.AsyncClient) -> None:
    # No Chromium is started in this fixture, so readiness must fail loudly
    # rather than reporting a service that cannot actually shop.
    response = await anon.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["session_state"] == "browser_down"


async def test_readyz_says_nothing_about_the_account(anon: httpx.AsyncClient) -> None:
    """It is unauthenticated, so the body must carry no account data at all."""
    body = (await anon.get("/readyz")).json()
    assert set(body) == {"status", "session_state"}


async def test_readyz_never_starts_a_browser(anon: httpx.AsyncClient, app: FastAPI) -> None:
    """An open endpoint must not be a lever for driving amazon.nl traffic."""
    browser = app.state.browser
    assert browser.is_running is False
    for _ in range(5):
        await anon.get("/readyz")
    assert browser.is_running is False


@pytest.mark.parametrize(
    "value",
    [b"Bearer \xc3\xa9\xc3\xa9", b"Bearer \xff\xfe\x00", b"\x80\x81"],
)
async def test_non_ascii_authorization_headers_are_rejected_not_crashed(
    anon: httpx.AsyncClient, value: bytes
) -> None:
    """A header may carry any byte; secrets.compare_digest refuses non-ASCII str.

    Comparing the raw bytes is what keeps this a 401 instead of a 500.
    """
    response = await anon.post("/mcp", json={}, headers=[(b"authorization", value)])
    assert response.status_code == 401


async def test_tools_are_reachable_over_the_real_streamable_http_transport(
    app: FastAPI, http_settings: Settings
) -> None:
    transport = httpx2.ASGITransport(app=app)
    http_client = httpx2.AsyncClient(
        transport=transport,
        base_url=BASE_URL,
        headers={"Authorization": f"Bearer {http_settings.auth_token}"},
        follow_redirects=True,
    )
    async with (
        http_client,
        Client(streamable_http_client(f"{BASE_URL}/mcp", http_client=http_client)) as client,
    ):
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert "amazon_search_products" in names
    assert "amazon_add_to_cart" in names
