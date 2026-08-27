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

import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

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
        self._token = token
        self._enabled = enabled and bool(token)

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:  # type: ignore[type-arg]
        if scope["type"] != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        provided = _bearer(headers.get("authorization", ""))
        if provided is None or self._token is None or not secrets.compare_digest(provided, self._token):
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


def _bearer(header: str) -> str | None:
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
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

    @app.get("/readyz")
    async def readyz() -> Response:
        """Readiness: Chromium is alive and the amazon.nl session is valid."""
        status = await client.session_status()
        code = 200 if status.state == "authenticated" else 503
        return JSONResponse(
            {
                "status": "ready" if code == 200 else "not_ready",
                "session_state": status.state,
                "cart_item_count": status.cart_item_count,
                "detail": status.detail,
            },
            status_code=code,
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
