"""The ASGI host: MCP over Streamable HTTP, mounted in FastAPI.

Layout of the served app::

    /healthz          liveness  — is the process up (no browser work)
    /readyz           readiness — is Chromium alive AND the amazon.nl login valid
    /mcp              the Streamable HTTP MCP endpoint

Health endpoints are deliberately unauthenticated so a systemd watchdog or a
container health check can reach them; they expose no account data. Everything
under the MCP path requires the bearer token.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
from starlette.routing import Mount
from starlette.types import ASGIApp

from amazon_nl_mcp.amazon.client import AmazonClient
from amazon_nl_mcp.browser import BrowserSession
from amazon_nl_mcp.config import Settings
from amazon_nl_mcp.logging import get_logger
from amazon_nl_mcp.models import SessionState
from amazon_nl_mcp.server import create_mcp_server

log = get_logger(__name__)

#: Paths reachable without a bearer token.
PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/"})


class BearerAuthMiddleware:
    """Static-bearer-token gate in front of the MCP endpoint.

    The MCP spec's authorization story is OAuth 2.1 against a real authorization
    server. That is the right answer for a multi-tenant deployment and the wrong
    one for a single-user box with no identity provider on it, so this is a
    constant-time comparison against one token from the environment. It is a
    lock on a door that already only opens onto loopback.
    """

    def __init__(self, app: ASGIApp, token: str | None, *, enabled: bool = True) -> None:
        self.app = app
        # Compared as bytes: an Authorization header may carry any byte, and
        # secrets.compare_digest refuses non-ASCII str arguments outright.
        self._expected = token.encode("utf-8") if token else None
        self._enabled = enabled and bool(token)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:  # type: ignore[type-arg]
        if scope["type"] != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        provided = _bearer(_header(scope, b"authorization"))
        if provided is None or self._expected is None or not secrets.compare_digest(provided, self._expected):
            log.warning("auth_rejected", path=scope.get("path"), client=str(scope.get("client")))
            response = JSONResponse(
                {
                    "error": "unauthorized",
                    "detail": "Send 'Authorization: Bearer <AMAZON_MCP_AUTH_TOKEN>'.",
                },
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="amazon-nl-mcp"'},
            )
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


def _header(scope: Mapping[str, Any], name: bytes) -> bytes:
    """Raw header bytes, without the lossy decode a str lookup would need."""
    headers: Iterable[tuple[bytes, bytes]] = scope.get("headers", ())
    for key, value in headers:
        if key.lower() == name:
            return value
    return b""


def _bearer(header: bytes) -> bytes | None:
    scheme, _, value = header.partition(b" ")
    if scheme.lower() != b"bearer" or not value.strip():
        return None
    return value.strip()


def _transport_security(settings: Settings) -> TransportSecuritySettings:
    """Host/Origin allowlist for MCP's DNS-rebinding protection.

    The SDK defaults to localhost only, which rejects a container reaching the
    host over the docker bridge with a bare 421. Anything the operator binds to
    has to be named here as well.
    """
    hosts = {
        "localhost",
        "localhost:*",
        "127.0.0.1",
        "127.0.0.1:*",
        f"{settings.host}",
        f"{settings.host}:{settings.port}",
        f"{settings.host}:*",
        *settings.allowed_hosts,
    }
    origins = {
        "http://localhost",
        "http://localhost:*",
        "http://127.0.0.1",
        "http://127.0.0.1:*",
        f"http://{settings.host}:{settings.port}",
        *settings.allowed_origins,
    }
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=sorted(hosts),
        allowed_origins=sorted(origins),
    )


class _Readiness:
    """TTL-cached readiness, so an open endpoint cannot become a traffic source."""

    def __init__(self, client: AmazonClient, browser: BrowserSession, *, ttl_s: float) -> None:
        self._client = client
        self._browser = browser
        self._ttl_s = ttl_s
        self._lock = asyncio.Lock()
        self._state: SessionState = "unknown"
        self._checked_at = 0.0

    async def state(self) -> SessionState:
        if not self._browser.is_running:
            # Never start Chromium on an unauthenticated request.
            return "browser_down"
        async with self._lock:
            if time.monotonic() - self._checked_at < self._ttl_s:
                return self._state
            self._state = (await self._client.session_status()).state
            self._checked_at = time.monotonic()
            return self._state


def build_app(settings: Settings) -> FastAPI:
    """Assemble the FastAPI app: browser lifecycle, health routes, mounted MCP."""
    browser = BrowserSession(settings)
    client = AmazonClient(browser, settings)
    mcp: MCPServer = create_mcp_server(client, settings)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path=settings.mcp_path,
        transport_security=_transport_security(settings),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # A mounted MCP app's own lifespan never runs, so the host must start the
        # session manager itself or the first request dies with
        # "Task group is not initialized".
        async with mcp.session_manager.run():
            try:
                await browser.start()
            except Exception as exc:
                # A missing browser is a degraded service, not a dead one:
                # /readyz will say so and `amazon-nl-mcp login` can fix it
                # without the operator having to restart the unit.
                log.error("browser_start_failed", error=str(exc))
            log.info(
                "service_ready",
                url=settings.public_url,
                auth="bearer" if not settings.auth_disabled else "disabled",
                write_enabled=settings.write_enabled,
            )
            try:
                yield
            finally:
                await browser.stop()
                log.info("service_stopped")

    app = FastAPI(
        title="amazon-nl-mcp",
        description="MCP server for searching amazon.nl and filling a personal cart.",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.browser = browser
    app.state.client = client
    app.state.mcp = mcp

    @app.get("/healthz")
    async def healthz() -> Response:
        """Liveness: the process is up. Does no browser work."""
        return JSONResponse(
            {
                "status": "ok",
                "browser_running": browser.is_running,
                "restarts": browser.restarts,
                "write_enabled": settings.write_enabled,
            }
        )

    readiness = _Readiness(client, browser, ttl_s=settings.readiness_ttl_s)

    @app.get("/readyz")
    async def readyz() -> Response:
        """Readiness: Chromium is alive and the amazon.nl session is valid.

        Unauthenticated, so it must not be a lever: the answer is cached, and a
        stopped browser is reported without starting one. A monitoring loop
        therefore cannot drive amazon.nl traffic or spend the shopping budget.
        The body says whether the service works and nothing about the account.
        """
        state = await readiness.state()
        ready = state == "authenticated"
        return JSONResponse(
            {"status": "ready" if ready else "not_ready", "session_state": state},
            status_code=200 if ready else 503,
        )

    @app.get("/")
    async def root() -> Response:
        """Where to point an MCP client. Intentionally says nothing about the account."""
        return JSONResponse(
            {
                "service": "amazon-nl-mcp",
                "mcp_endpoint": settings.mcp_path,
                "transport": "streamable-http",
                "auth": "disabled" if settings.auth_disabled else "bearer",
            }
        )

    # Mount at the root with the MCP endpoint keeping its own full path: mounting
    # at "/mcp" instead would make Starlette redirect /mcp -> /mcp/, which MCP
    # clients do not follow on a POST. Appended last so the routes declared above
    # still match first.
    app.router.routes.append(Mount("/", app=mcp_app))
    app.add_middleware(
        BearerAuthMiddleware,
        token=settings.auth_token,
        enabled=not settings.auth_disabled,
    )
    return app


AppFactory = Callable[[], Awaitable[FastAPI]]
