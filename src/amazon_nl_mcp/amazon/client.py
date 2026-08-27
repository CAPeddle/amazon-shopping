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
from typing import Literal, TypedDict
from urllib.parse import quote_plus, urlencode

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
    BrowserUnavailableError,
    CartVerificationError,
    NetworkError,
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

#: Rows read from one search page. Also the ceiling on `limit`, so that "raise
#: limit to see the rest of this page" is always advice a caller can take.
MAX_ROWS_PER_PAGE = 60

#: Deepest results page worth offering; Amazon's own relevance is long gone by here.
MAX_SEARCH_PAGE = 20

WaitState = Literal["commit", "domcontentloaded", "load", "networkidle"]


class _StatusBase(TypedDict):
    """Fields every :class:`SessionStatus` carries regardless of outcome."""

    storefront: str
    profile_dir: str
    write_enabled: bool


def _quantity_of(asin: str, cart: Cart) -> int:
    """Units of one ASIN in a cart, summed across duplicate lines."""
    return sum(line.quantity for line in cart.lines if (line.asin or "").upper() == asin.upper())


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
        url = f"{self.base_url}/s?k={quote_plus(query)}&language=nl_NL"
        return url if page_number <= 1 else f"{url}&page={page_number}"

    def product_url(self, asin: str, *, localised: bool = False) -> str:
        url = f"{self.base_url}/dp/{asin}"
        return f"{url}?language=nl_NL" if localised else url

    @property
    def cart_url(self) -> str:
        return f"{self.base_url}/gp/cart/view.html"

    def add_to_cart_url(self, asin: str, quantity: int) -> str:
        """The legacy associates add endpoint.

        One GET, session cookies attached, no CSRF token to scrape and no
        80-request product page to render. It has outlived a decade of Amazon
        redesigns, which is exactly why it is the primary path here.
        """
        params = urlencode({"ASIN.1": asin, "Quantity.1": str(quantity)})
        return f"{self.base_url}/gp/aws/cart/add.html?{params}"

    # -- navigation primitives -------------------------------------------

    async def _goto(self, page: Page, url: str, *, wait: WaitState = "domcontentloaded") -> None:
        """Navigate, then screen the response for Amazon's automation checks."""
        try:
            await page.goto(url, wait_until=wait, timeout=self._settings.nav_timeout_ms)
        except PlaywrightTimeout as exc:
            raise PageTimeoutError(
                f"amazon.nl did not finish loading within {self._settings.nav_timeout_ms // 1000}s.",
                hint="Retry; if it keeps timing out, the browser profile may need a fresh login.",
            ) from exc
        except PlaywrightError as exc:
            # Playwright appends a multi-line call log; only the first line says
            # anything the caller can act on.
            reason = str(exc).splitlines()[0]
            raise NetworkError(
                f"Could not reach amazon.nl: {reason}",
                hint="Check this host's connectivity and any outbound proxy, then retry.",
            ) from exc

        # Screen for the automation check *before* clicking anything: a captcha
        # page has a submit button too, and submitting an empty one is worse
        # than useless.
        await self._raise_if_blocked(page)
        await self._dismiss_consent(page)

    async def _dismiss_consent(self, page: Page) -> None:
        """Click away the cookie banner and the soft "continue shopping" wall.

        Both are one-time per profile in practice, but while they are up they
        steal clicks from the add-to-cart button, so this runs on every page.
        Only reached once :meth:`_raise_if_blocked` has cleared the page.
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
        if not await self.is_signed_in(page):
            raise NotLoggedInError()

    async def is_signed_in(self, page: Page) -> bool:
        """Whether ``page`` shows an authenticated amazon.nl session.

        Three independent tells, cheapest and strongest first.

        ``data-nav-role="signin"`` is present on the account link only while
        signed out, and disappears once the session is authenticated. The
        greeting text is matched *exactly*, never by substring: "Hallo" prefixes
        both "Hallo, inloggen" and "Hallo, <name>".
        """
        if "/ap/signin" in page.url or await X.exists(page, S.SIGN_IN_PAGE_MARKERS):
            return False
        if await X.exists(page, [S.NAV_SIGNIN_ROLE]):
            return False
        href = await X.first_attr(page, [S.NAV_ACCOUNT_LINK], "href")
        if href and "/ap/signin" in href:
            return False
        greeting = await X.first_text(page, S.NAV_ACCOUNT_GREETING)
        if greeting is None:
            # No nav at all: a wall, or a page shape we do not know. Not a
            # signed-in state either way.
            return False
        return greeting.strip().lower() not in S.SIGNED_OUT_MARKERS

    async def _cart_count(self, page: Page) -> int:
        return await X.read_cart_count(page)

    def _mark_success(self) -> None:
        self.last_success_at = time.time()

    # -- read operations ---------------------------------------------------

    async def session_status(self, *, reveal_account: bool = False) -> SessionStatus:
        """Report whether the stored browser session can still act on the account.

        Never raises for an expected failure: the whole point of this tool is to
        answer "why is nothing working?", so a bot wall or a dead browser is a
        *state*, not an exception.
        """
        base: _StatusBase = {
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
            async with asyncio.timeout(self._settings.status_timeout_s):
                return await self._probe_session(reveal_account=reveal_account, base=base)
        except TimeoutError:
            return SessionStatus(
                state="unknown",
                signed_in=False,
                detail=(
                    f"Checking the session took longer than {self._settings.status_timeout_s:.0f}s. "
                    "amazon.nl may be slow or the browser may be wedged; "
                    "`systemctl --user restart amazon-nl-mcp` if it persists."
                ),
                **base,
            )
        except BotWallError as exc:
            return SessionStatus(state="blocked", signed_in=False, detail=exc.as_text(), **base)
        except NetworkError as exc:
            return SessionStatus(state="unreachable", signed_in=False, detail=exc.as_text(), **base)
        except BrowserUnavailableError as exc:
            return SessionStatus(state="browser_down", signed_in=False, detail=exc.as_text(), **base)
        except AmazonMCPError as exc:
            return SessionStatus(state="unknown", signed_in=False, detail=exc.as_text(), **base)

    async def _probe_session(self, *, reveal_account: bool, base: _StatusBase) -> SessionStatus:
        """The page work behind :meth:`session_status`, without the error mapping."""
        # Deliberately not rate limited: diagnosing a service that is refusing
        # work must not itself be refused for the same reason.
        async with self._browser.page(rate_limited=False) as page:
            await self._goto(page, self.base_url)
            signed_in = await self.is_signed_in(page)
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
            # Read the whole page, then truncate: that is the only way to know
            # whether `limit` hid anything, and to filter ads without the filter
            # eating into the caller's budget.
            found = await X.extract_search_rows(page, self.base_url, MAX_ROWS_PER_PAGE)
            if not include_sponsored:
                found = [p for p in found if not p.is_sponsored]
            products = found[:limit]
            truncated = len(found) > limit

            more_pages = await X.exists(page, S.SEARCH_NEXT_PAGE)
            has_more = truncated or more_pages
            if not products:
                text = await X.page_text_lower(page, limit=4_000)
                if any(marker in text for marker in S.NO_RESULTS_MARKERS):
                    has_more = False
                    more_pages = False
                elif not await self.is_signed_in(page):
                    raise NotLoggedInError()
            self._mark_success()
            log.info("search_completed", query_length=len(query), count=len(products), page=page_number)
            return SearchResults(
                query=query,
                count=len(products),
                page=page_number,
                has_more=has_more,
                truncated=truncated,
                # Advertised only when the caller has seen this page out and
                # there is a page to advertise; otherwise "next page" would mean
                # "skip whatever limit hid". `limit` reaches MAX_ROWS_PER_PAGE,
                # so raising it is always a way out of a truncated page.
                next_page=(
                    page_number + 1
                    if (more_pages and not truncated and page_number < MAX_SEARCH_PAGE)
                    else None
                ),
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
        await self._goto(page, self.product_url(asin, localised=True))
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

        # /dp/<child> can silently resolve to a variation parent; the hidden
        # input says which ASIN the buy box actually belongs to.
        requested = asin.upper()
        resolved = (await X.first_attr(page, S.PDP_ASIN_INPUT, "value") or "").strip().upper()
        if X.is_valid_asin(resolved) and resolved != requested:
            log.info("asin_redirected", requested=requested, resolved=resolved)
            asin = resolved

        availability = await X.first_text(page, S.PDP_AVAILABILITY)
        unavailable = await X.exists(page, S.PDP_OUT_OF_STOCK) or any(
            marker in (availability or "").lower() for marker in S.UNAVAILABLE_TEXT_MARKERS
        )

        bullets = await self._read_bullets(page)
        return ProductDetail(
            asin=asin.upper(),
            resolved_from=requested if asin.upper() != requested else None,
            title=title,
            url=self.product_url(asin.upper()),
            price=X.parse_price(await X.first_text(page, S.PDP_PRICE_DISPLAY)),
            availability=availability,
            in_stock=has_add_button and not unavailable,
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

        Two mechanisms, in order:

        1. ``/gp/aws/cart/add.html?ASIN.1=..&Quantity.1=..`` — one authenticated
           GET, no CSRF token to scrape, no 80-request page render. This is the
           primary path.
        2. clicking ``#add-to-cart-button`` on the product page, as the fallback
           for listings the legacy endpoint refuses.

        Either way the result is read back off the **cart page**. Amazon renders
        a success banner for adds that changed nothing (a variation parent, a
        listing that sold out between load and click), so the banner is never
        the evidence.
        """
        asin = asin.strip().upper()
        wanted = max(1, min(quantity, MAX_PDP_QUANTITY))

        async with self._browser.page() as page:
            detail = await self._load_product(page, asin)
            await self._require_signed_in(page)

            if detail.requires_variant_selection:
                raise VariantSelectionRequiredError(asin, [v.label for v in detail.variants])
            if not detail.in_stock:
                raise ProductUnavailableError(
                    f"{detail.title!r} ({asin}) cannot be added on amazon.nl"
                    + (f" — availability reads {detail.availability!r}." if detail.availability else "."),
                    hint="Check amazon_get_product for variants, or pick another listing.",
                )
            # The buy box may belong to a different ASIN than the one asked for
            # (a variation parent resolving to its default child, a discontinued
            # listing redirecting to its replacement). Follow it — that is what a
            # person clicking the same link would get — but never silently.
            requested, asin = asin, detail.asin
            substituted = requested != asin

            baseline = _quantity_of(asin, await self._read_cart(page))

            mechanism = "add_url"
            await self._goto(page, self.add_to_cart_url(asin, wanted))
            await self._check_variant_interstitial(page, asin, detail)
            cart = await self._read_cart_settled(page, asin, baseline)

            if _quantity_of(asin, cart) <= baseline:
                log.info("add_url_ineffective_falling_back", asin=asin)
                mechanism = "pdp_click"
                await self._add_via_product_page(page, asin, wanted)
                cart = await self._read_cart_settled(page, asin, baseline)

            in_cart = _quantity_of(asin, cart)
            if in_cart <= baseline:
                raise CartVerificationError(
                    f"Neither add mechanism put {asin} in the cart.",
                    hint=(
                        f"Open {self.cart_url} in a browser to check. This usually means the listing "
                        "needs a variant chosen, is sold by a seller that will not ship here, or the "
                        "session expired mid-request."
                    ),
                )

            self._mark_success()
            added = in_cart - baseline
            complete = added >= quantity
            message = (
                f"Added {added}x {detail.title!r} to the amazon.nl cart via {mechanism} "
                f"({in_cart} of this item now in the cart, {cart.item_count} items total). "
                f"Review and check out yourself at {self.cart_url} — this service never places orders."
            )
            if substituted:
                message = (f"amazon.nl resolved {requested} to {asin} and that is what was added. ") + message

            log.info("added_to_cart", asin=asin, requested=quantity, in_cart=in_cart, mechanism=mechanism)
            if not complete:
                message += f" Only {added} of the {quantity} requested landed; call again for the rest."
            return AddToCartResult(
                ok=complete,
                asin=asin,
                resolved_from=requested if substituted else None,
                title=detail.title,
                requested_quantity=quantity,
                added_quantity=added,
                quantity_in_cart=in_cart,
                price=detail.price,
                cart_item_count=cart.item_count,
                cart_url=self.cart_url,
                message=message,
            )

    async def _read_cart_settled(self, page: Page, asin: str, baseline: int) -> Cart:
        """Re-read the cart until it reflects the add, or until we give up.

        Amazon's cart is briefly stale straight after an add. Treating the first
        stale read as failure would send the fallback mechanism at a product
        that is already in the cart — and add it twice.
        """
        cart = await self._read_cart(page)
        for attempt in range(2):
            if _quantity_of(asin, cart) > baseline:
                return cart
            await asyncio.sleep(1.0 + attempt)
            cart = await self._read_cart(page)
        return cart

    async def _check_variant_interstitial(self, page: Page, asin: str, detail: ProductDetail) -> None:
        """The add endpoint answers a variation parent with a 'Kies een optie' page."""
        text = await X.page_text_lower(page, limit=4_000)
        if any(marker in text for marker in S.VARIANT_REQUIRED_MARKERS):
            raise VariantSelectionRequiredError(asin, [v.label for v in detail.variants])

    async def _add_via_product_page(self, page: Page, asin: str, quantity: int) -> None:
        """Fallback: drive the product page's own add-to-cart form."""
        await self._goto(page, self.product_url(asin, localised=True))
        applied = await self._set_pdp_quantity(page, quantity)
        await self._click_add_to_cart(page)
        await self._dismiss_upsells(page)
        if applied < quantity:
            log.info("pdp_quantity_capped", asin=asin, wanted=quantity, applied=applied)

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
        """Decline the protection-plan side sheet.

        The item is already in the cart by the time this appears — the sheet is
        an upsell, not a gate — so a failure to find it is not a failure to add.
        """
        for selector in S.WARRANTY_DECLINE:
            try:
                locator = page.locator(selector).first
                if await locator.count() and await locator.is_visible():
                    await locator.click(timeout=3_000)
                    with contextlib.suppress(PlaywrightTimeout):
                        await page.wait_for_load_state("domcontentloaded", timeout=10_000)
                    return
            except PlaywrightError:
                continue
