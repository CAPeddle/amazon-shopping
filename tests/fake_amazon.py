"""A stand-in amazon.nl, served entirely from ``tests/fixtures``.

Every navigation the client makes is intercepted with ``page.route`` and
fulfilled from a saved page, so the code under test is the *real* client: the
same ``_goto``, the same consent handling, the same bot-wall screen, the same
extraction. Only the bytes on the wire are ours.

The stub is stateful on purpose. ``/gp/cart/view.html`` answers with a different
cart before and after the add endpoint is hit, which is what makes the
verify-by-re-reading-the-cart logic actually testable — a static cart would let
a broken implementation pass.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import BrowserContext, Page, Route

from amazon_nl_mcp.browser import CircuitBreaker, RateLimiter
from amazon_nl_mcp.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"

#: Amazon's "dog page". Note it shares wording with the bot wall, which is why
#: the captcha markers must not match on it.
NOT_FOUND = (
    "<html lang='nl'><body><h1>Sorry! Er is iets misgegaan.</h1>"
    "<p>De pagina die je zoekt bestaat niet.</p></body></html>"
)


@dataclass
class FakeAmazon:
    """Routing table plus the little state the cart flow needs."""

    #: URL pattern -> fixture filename, checked in order.
    routes: list[tuple[re.Pattern[str], str]] = field(default_factory=list)
    #: Cart fixture served before and after the add endpoint is hit.
    cart_before: str = "cart_before_add.html"
    cart_after: str = "cart_after_add.html"
    #: Set to a fixture name to make the add endpoint answer with it.
    add_response: str = "add_confirmation.html"
    #: When False the add endpoint is a no-op, so the cart never changes.
    add_succeeds: bool = True
    #: When True every navigation fails at the transport, as if the box were offline.
    offline: bool = False

    requests: list[str] = field(default_factory=list)
    add_hits: int = 0

    def route(self, pattern: str, fixture: str) -> FakeAmazon:
        self.routes.insert(0, (re.compile(pattern), fixture))
        return self

    def _fixture_for(self, url: str) -> str | None:
        if "/gp/aws/cart/add.html" in url:
            self.add_hits += 1
            return self.add_response
        if "/gp/cart/view.html" in url:
            return self.cart_after if (self.add_hits and self.add_succeeds) else self.cart_before
        for pattern, fixture in self.routes:
            if pattern.search(url):
                return fixture
        return None

    async def handle(self, route: Route) -> None:
        request = route.request
        if self.offline:
            await route.abort("connectionfailed")
            return
        if request.resource_type != "document":
            # Images, CSS and Amazon's own beacons: never fetched, never needed.
            await route.abort()
            return
        self.requests.append(request.url)
        fixture = self._fixture_for(request.url)
        if fixture is None:
            await route.fulfill(status=404, content_type="text/html; charset=utf-8", body=NOT_FOUND)
            return
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            path=str(FIXTURES / fixture),
        )

    async def install(self, page: Page) -> None:
        # Registered last wins in Playwright, so the catch-all goes on first:
        # nothing in this suite is allowed to leave the machine.
        await page.route("**/*", lambda r: r.abort())
        await page.route("**://*.amazon.nl/**", self.handle)


def default_fake() -> FakeAmazon:
    """The routing table the happy-path tests share."""
    return FakeAmazon(
        routes=[
            (re.compile(r"/s\?.*\bk="), "search_results.html"),
            (re.compile(r"/dp/B0CX23V2ZK"), "product_detail.html"),
            (re.compile(r"/dp/B0PARENT01"), "product_variant_parent.html"),
            (re.compile(r"/dp/B0GONE0001"), "product_out_of_stock.html"),
            (re.compile(r"amazon\.nl/?$"), "product_detail.html"),
        ]
    )


class StubBrowserSession:
    """A :class:`~amazon_nl_mcp.browser.BrowserSession` shaped enough for the client.

    It hands out pages from one browser context with the fake wired in, and
    reuses the production guard objects so their behaviour is under test too.
    """

    def __init__(self, context: BrowserContext, fake: FakeAmazon, settings: Settings) -> None:
        self._context = context
        self._fake = fake
        self.rate_limiter = RateLimiter(settings.rate_limit_per_minute)
        self.breaker = CircuitBreaker(settings.bot_wall_cooldown_s)
        self.restarts = 0
        self.pages_opened = 0

    @property
    def is_running(self) -> bool:
        return True

    @asynccontextmanager
    async def page(self, *, rate_limited: bool = True) -> AsyncIterator[Page]:
        self.breaker.raise_if_open()
        if rate_limited:
            self.rate_limiter.check()
        page = await self._context.new_page()
        self.pages_opened += 1
        await self._fake.install(page)
        try:
            yield page
        finally:
            await page.close()
