"""Shared fixtures.

The parser tests run against a *real* Chromium loaded with saved amazon.nl HTML
via ``page.set_content()``. That is deliberate: the production code path uses
Playwright locators (including ``:has-text`` and ``:not()`` chains that a
BeautifulSoup port would have to reimplement), so testing it through anything
else would test a different program.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from playwright.async_api import Browser, Page, async_playwright

from amazon_nl_mcp.config import Settings

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """One asyncio loop for the whole session; Playwright objects cross tests."""
    return "asyncio"


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        auth_token="test-token-abcdefghijklmnop",
        profile_dir=Path("/tmp/amazon-nl-mcp-tests/profile"),
        rate_limit_per_minute=600,
    )


@pytest.fixture(scope="session")
async def browser() -> AsyncIterator[Browser]:
    """One plain (non-persistent) Chromium for the whole parser suite."""
    async with async_playwright() as pw:
        launched = await pw.chromium.launch(
            headless=True,
            executable_path=os.environ.get("AMAZON_MCP_BROWSER_EXECUTABLE_PATH") or None,
            # Tests may run as root (CI containers), where Chromium's sandbox
            # cannot start. Nothing untrusted is loaded here — only local fixtures.
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            yield launched
        finally:
            await launched.close()


@pytest.fixture
async def page(browser: Browser) -> AsyncIterator[Page]:
    context = await browser.new_context(locale="nl-NL")
    opened = await context.new_page()
    try:
        yield opened
    finally:
        await context.close()


PageLoader = Callable[[str], Awaitable[Page]]


@pytest.fixture
def fixture_page(page: Page) -> PageLoader:
    """Return a loader that puts a named fixture into the page."""

    async def _load(name: str) -> Page:
        await page.set_content(load_fixture(name), wait_until="domcontentloaded")
        return page

    return _load
