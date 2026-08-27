"""Page flows against amazon.nl.

This is the only module that navigates. It owns the rules that make the
difference between a scraper that works once and a service that keeps working:

* every navigation is screened for Amazon's automation checks, and one trips the
  circuit breaker for the whole process;
* every mutation is **verified against the cart**, never against the success
  banner Amazon renders (which appears for adds that silently did nothing);
* an expired login is detected and reported as an actionable state rather than
  surfacing as an empty page.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import UTC, datetime
from urllib.parse import quote_plus

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from amazon_nl_mcp.amazon import extract as X
from amazon_nl_mcp.amazon import selectors as S
from amazon_nl_mcp.browser import BrowserSession
from amazon_nl_mcp.config import Settings
from amazon_nl_mcp.errors import (
    AmazonMCPError,
    BotWallError,
    CartVerificationError,
    NotLoggedInError,
    PageTimeoutError,
    ProductNotFoundError,
    ProductUnavailableError,
    VariantSelectionRequiredError,
)
from amazon_nl_mcp.logging import get_logger
from amazon_nl_mcp.models import (
    AddToCartResult,
    Cart,
    ProductDetail,
    SearchResults,
    SessionStatus,
)

log = get_logger(__name__)

#: Amazon's quantity dropdown tops out here; larger orders need the cart page.
MAX_PDP_QUANTITY = 30


class AmazonClient:
    """High-level operations on the signed-in amazon.nl storefront."""

    def __init__(self, browser: BrowserSession, settings: Settings) -> None:
        self._browser = browser
        self._settings = settings
        self.last_success_at: float | None = None

    # -- URLs -------------------------------------------------------------

    @property
    def base_url(self) -> str:
        return self._settings.base_url

    def search_url(self, query: str, page_number: int = 1) -> str:
        url = f"{self.base_url}/s?k={quote_plus(query)}"
        return url if page_number <= 1 else f"{url}&page={page_number}"

    def product_url(self, asin: str) -> str:
        return f"{self.base_url}/dp/{asin}"

    @property
    def cart_url(self) -> str:
        return f"{self.base_url}/gp/cart/view.html"

    # -- navigation primitives -------------------------------------------

    async def _goto(self, page: Page, url: str, *, wait: str = "domcontentloaded") -> None:
        """Navigate, then screen the response for Amazon's automation checks."""
        try:
            await page.goto(url, wait_until=wait, timeout=self._settings.nav_timeout_ms)
        except PlaywrightTimeout as exc:
            raise PageTimeoutError(
                f"amazon.nl did not finish loading within {self._settings.nav_timeout_ms // 1000}s.",
                hint="Retry; if it keeps timing out, the browser profile may need a fresh login.",
            ) from exc
        except PlaywrightError as exc:
            raise AmazonMCPError(f"Navigation to amazon.nl failed: {exc}") from exc

        await self._dismiss_consent(page)
        await self._raise_if_blocked(page)

    async def _dismiss_consent(self, page: Page) -> None:
        """Click away the cookie banner and the delivery-location popover.

        Both are one-time per profile in practice, but they steal clicks from
        the add-to-cart button when they are up, so this runs on every page.
        """
        for candidates in (S.COOKIE_ACCEPT, S.CONTINUE_SHOPPING):
            for selector in candidates:
                try:
                    locator = page.locator(selector).first
                    if await locator.count() and await locator.is_visible():
                        await locator.click(timeout=3_000)
                        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                        break
                except PlaywrightError:
                    continue

    async def _raise_if_blocked(self, page: Page) -> None:
        """Trip the breaker and raise if this page is a CAPTCHA / bot wall."""
        blocked = await X.exists(page, S.CAPTCHA_MARKERS)
        if not blocked:
            text = await X.page_text_lower(page, limit=3_000)
            blocked = any(marker in text for marker in S.CAPTCHA_TEXT_MARKERS)
        if blocked:
            self._browser.breaker.trip()
            raise BotWallError()
        self._browser.breaker.reset()

    async def _require_signed_in(self, page: Page) -> None:
        """Raise :class:`NotLoggedInError` unless the nav shows a signed-in account."""
        if not await self._is_signed_in(page):
            raise NotLoggedInError()

    async def _is_signed_in(self, page: Page) -> bool:
        if await X.exists(page, S.SIGN_IN_PAGE_MARKERS):
            return False
        greeting = await X.first_text(page, S.NAV_ACCOUNT_GREETING)
        if greeting is None:
            return False
        return not any(marker == greeting.strip().lower() for marker in S.SIGNED_OUT_MARKERS) and not any(
            greeting.strip().lower().endswith(marker) for marker in S.SIGNED_OUT_MARKERS
        )

    async def _cart_count(self, page: Page) -> int:
        return X.parse_int(await X.first_text(page, S.NAV_CART_COUNT)) or 0

    def _mark_success(self) -> None:
        self.last_success_at = time.time()

    # -- read operations ---------------------------------------------------

    async def session_status(self, *, reveal_account: bool = False) -> SessionStatus:
        """Report whether the stored browser session can still act on the account.

        Never raises for an expected failure: the whole point of this tool is to
        answer "why is nothing working?", so a bot wall or a dead browser is a
        *state*, not an exception.
        """
        base = {
            "storefront": self.base_url,
            "profile_dir": str(self._settings.profile_dir),
            "write_enabled": self._settings.write_enabled,
        }
        if self._browser.breaker.is_open:
            return SessionStatus(
                state="blocked",
                signed_in=False,
                detail=(
                    "amazon.nl is serving an automation check; the service is backing off for "
                    f"{self._browser.breaker.remaining_s():.0f}s. Run `amazon-nl-mcp login` to clear it by hand."
                ),
                **base,
            )
        try:
            async with self._browser.page(rate_limited=False) as page:
                await self._goto(page, self.base_url)
                signed_in = await self._is_signed_in(page)
                greeting = await X.first_text(page, S.NAV_ACCOUNT_GREETING)
                count = await self._cart_count(page)
                self._mark_success()
                return SessionStatus(
                    state="authenticated" if signed_in else "signed_out",
                    signed_in=signed_in,
                    account_label=greeting if (signed_in and reveal_account) else None,
                    cart_item_count=count,
                    detail=(
                        "Signed in; search and cart tools are available."
                        if signed_in
                        else "Signed out. Run `amazon-nl-mcp login` on the host to sign in interactively."
                    ),
                    **base,
                )
        except BotWallError as exc:
            return SessionStatus(state="blocked", signed_in=False, detail=exc.as_text(), **base)
        except AmazonMCPError as exc:
            return SessionStatus(state="browser_down", signed_in=False, detail=exc.as_text(), **base)

    async def search_products(
        self,
        query: str,
        *,
        limit: int | None = None,
        page_number: int = 1,
        include_sponsored: bool = False,
    ) -> SearchResults:
        """Run a storefront search and read the result rows."""
        limit = limit or self._settings.max_results
        url = self.search_url(query, page_number)
        async with self._browser.page() as page:
            await self._goto(page, url)
            # Over-fetch when filtering ads out, so a page of sponsored rows
            # still yields a full page of organic results.
            fetch = limit if include_sponsored else min(limit * 3, 60)
            products = await X.extract_search_rows(page, self.base_url, fetch)
            if not include_sponsored:
                products = [p for p in products if not p.is_sponsored]
            products = products[:limit]

            has_more = await X.exists(page, S.SEARCH_NEXT_PAGE)
            if not products:
                text = await X.page_text_lower(page, limit=4_000)
                if any(marker in text for marker in S.NO_RESULTS_MARKERS):
                    has_more = False
                elif not await self._is_signed_in(page):
                    raise NotLoggedInError()
            self._mark_success()
            log.info("search_completed", query_length=len(query), count=len(products), page=page_number)
            return SearchResults(
                query=query,
                count=len(products),
                page=page_number,
                has_more=has_more,
                next_page=page_number + 1 if has_more else None,
                results_url=url,
                products=products,
            )

    async def get_product(self, asin: str) -> ProductDetail:
        """Load one product detail page."""
        async with self._browser.page() as page:
            detail = await self._load_product(page, asin)
            self._mark_success()
            return detail

    async def _load_product(self, page: Page, asin: str) -> ProductDetail:
        await self._goto(page, self.product_url(asin))
        title = await X.first_text(page, S.PDP_TITLE)
        if not title:
            text = await X.page_text_lower(page, limit=3_000)
            if any(marker in text for marker in S.PDP_DOG_PAGE_MARKERS) or not await X.exists(
                page, S.PDP_TITLE
            ):
                raise ProductNotFoundError(asin)
            raise ProductNotFoundError(asin)

        variants = await X.extract_variants(page)
        has_add_button = await X.exists(page, S.PDP_ADD_TO_CART)
        page_text = await X.page_text_lower(page, limit=8_000)
        needs_variant = not has_add_button and (
            bool(variants) or any(m in page_text for m in S.VARIANT_REQUIRED_MARKERS)
        )

        bullets = await self._read_bullets(page)
        return ProductDetail(
            asin=asin.upper(),
            title=title,
            url=self.product_url(asin.upper()),
            price=X.parse_price(await X.first_text(page, S.PDP_PRICE_DISPLAY)),
            availability=await X.first_text(page, S.PDP_AVAILABILITY),
            in_stock=has_add_button,
            rating=X.parse_rating(
                await X.first_text(page, S.PDP_RATING) or await X.first_attr(page, S.PDP_RATING, "title")
            ),
            review_count=X.parse_int(await X.first_text(page, S.PDP_REVIEW_COUNT)),
            image_url=await X.first_attr(page, S.PDP_IMAGE, "src"),
            brand=await X.first_text(page, S.PDP_BYLINE),
            bullets=bullets,
            variants=variants,
            requires_variant_selection=needs_variant,
        )

    async def _read_bullets(self, page: Page, limit: int = 8) -> list[str]:
        for selector in S.PDP_BULLETS:
            try:
                items = page.locator(selector)
                count = await items.count()
            except PlaywrightError:
                continue
            if count == 0:
                continue
            texts: list[str] = []
            for index in range(min(count, limit)):
                text = await items.nth(index).text_content()
                cleaned = " ".join((text or "").split())
                if cleaned and not cleaned.lower().startswith("zorg ervoor dat dit"):
                    texts.append(cleaned)
            if texts:
                return texts
        return []

    async def view_cart(self) -> Cart:
        """Read the current cart."""
        async with self._browser.page() as page:
            cart = await self._read_cart(page)
            self._mark_success()
            return cart

    async def _read_cart(self, page: Page) -> Cart:
        await self._goto(page, self.cart_url)
        await self._require_signed_in(page)
        lines = await X.extract_cart_lines(page, self.base_url)
        subtotal = X.parse_price(await X.first_text(page, S.CART_SUBTOTAL))
        count = X.parse_int(await X.first_text(page, S.CART_ITEM_COUNT_TEXT))
        if count is None:
            count = await self._cart_count(page)
        if not lines:
            text = await X.page_text_lower(page, limit=4_000)
            if any(marker in text for marker in S.CART_EMPTY_MARKERS):
                count = 0
        return Cart(
            item_count=count or sum(line.quantity for line in lines),
            line_count=len(lines),
            subtotal=subtotal,
            lines=lines,
            cart_url=self.cart_url,
        )

    # -- write operations --------------------------------------------------

    async def add_to_cart(self, asin: str, quantity: int = 1) -> AddToCartResult:
        """Add ``quantity`` of ``asin`` to the personal cart and verify it landed.

        The verification is the point. Amazon will happily render a success
        banner for an add that did not change the cart (a variant parent, a
        listing that went out of stock between load and click), so the result
        this returns is read back from the cart page, not from the banner.
        """
        asin = asin.strip().upper()
        async with self._browser.page() as page:
            detail = await self._load_product(page, asin)
            await self._require_signed_in(page)

            if detail.requires_variant_selection:
                raise VariantSelectionRequiredError(asin, [v.label for v in detail.variants])
            if not detail.in_stock:
                raise ProductUnavailableError(
                    f"{detail.title!r} ({asin}) has no add-to-cart control on amazon.nl"
                    + (f" — availability reads {detail.availability!r}." if detail.availability else "."),
                    hint="Use amazon_get_product to check availability, or pick another listing.",
                )

            before = await self._quantity_in_cart(asin)
            applied = await self._set_pdp_quantity(page, quantity)
            await self._click_add_to_cart(page)
            await self._dismiss_upsells(page)

            after, cart = await self._verify_added(asin, expected_min=before + applied)
            self._mark_success()

            added = after - before
            message = (
                f"Added {added}x {detail.title!r} to the amazon.nl cart "
                f"({after} now in the cart, {cart.item_count} items total). "
                f"Review and check out yourself at {self.cart_url} — this service never places orders."
            )
            if applied < quantity:
                message += (
                    f" Only {applied} could be added in one go (Amazon's dropdown caps at "
                    f"{MAX_PDP_QUANTITY}); call again for the rest."
                )
            log.info("added_to_cart", asin=asin, requested=quantity, in_cart=after)
            return AddToCartResult(
                ok=True,
                asin=asin,
                title=detail.title,
                requested_quantity=quantity,
                quantity_in_cart=after,
                price=detail.price,
                cart_item_count=cart.item_count,
                cart_url=self.cart_url,
                message=message,
            )

    async def _set_pdp_quantity(self, page: Page, quantity: int) -> int:
        """Set the quantity dropdown; return the quantity actually selected."""
        wanted = max(1, min(quantity, MAX_PDP_QUANTITY))
        if wanted == 1:
            return 1
        select = await X.first_locator(page, S.PDP_QUANTITY_SELECT)
        if select is None:
            return 1
        try:
            await select.select_option(str(wanted), timeout=5_000)
            return wanted
        except PlaywrightError:
            log.info("quantity_select_unavailable", wanted=wanted)
            return 1

    async def _click_add_to_cart(self, page: Page) -> None:
        button = await X.first_locator(page, S.PDP_ADD_TO_CART)
        if button is None:
            raise ProductUnavailableError("The add-to-cart button disappeared before it could be clicked.")
        try:
            await button.click(timeout=10_000)
        except PlaywrightError as exc:
            raise ProductUnavailableError(f"Could not click add-to-cart: {exc}") from exc
        with contextlib.suppress(PlaywrightTimeout):
            await page.wait_for_load_state("domcontentloaded", timeout=15_000)
        await self._raise_if_blocked(page)

    async def _dismiss_upsells(self, page: Page) -> None:
        """Decline the protection-plan / warranty interstitial if Amazon shows one."""
        for selector in S.WARRANTY_DECLINE:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=3_000)
                    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    return
            except PlaywrightError:
                continue

    async def _quantity_in_cart(self, asin: str) -> int:
        """How many units of ``asin`` the cart currently holds."""
        async with self._browser.page(rate_limited=False) as page:
            cart = await self._read_cart(page)
        return sum(line.quantity for line in cart.lines if (line.asin or "").upper() == asin)

    async def _verify_added(self, asin: str, *, expected_min: int) -> tuple[int, Cart]:
        """Re-read the cart until it shows the item, or give up and say so.

        Amazon's cart is eventually consistent for a second or two after an add,
        so one miss is not a failure; three is.
        """
        last: Cart | None = None
        for attempt in range(3):
            async with self._browser.page(rate_limited=False) as page:
                cart = await self._read_cart(page)
            last = cart
            found = sum(line.quantity for line in cart.lines if (line.asin or "").upper() == asin)
            if found >= expected_min or (found > 0 and attempt == 2):
                return found, cart
            await asyncio.sleep(1.0 + attempt)
        found = sum(line.quantity for line in last.lines if (line.asin or "").upper() == asin) if last else 0
        if found:
            return found, last  # type: ignore[return-value]
        raise CartVerificationError(
            f"Amazon accepted the add for {asin} but the cart does not show it.",
            hint=(
                f"Open {self.cart_url} in a browser to check. This can happen when the listing "
                "requires a variant, is sold by a blocked seller, or the session expired mid-request."
            ),
        )


def utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
