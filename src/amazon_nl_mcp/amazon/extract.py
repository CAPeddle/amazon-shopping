"""Turn amazon.nl DOM into the models in :mod:`amazon_nl_mcp.models`.

Everything here takes a Playwright ``Page`` or ``Locator`` and nothing else, so
the exact same code runs against a live storefront and against a saved fixture
loaded with ``page.set_content()``. That is what makes the selector table
testable without hitting Amazon.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page

from amazon_nl_mcp.amazon import selectors as S
from amazon_nl_mcp.models import CartLine, Money, ProductSummary, ProductVariant

_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aw/d)/([A-Z0-9]{10})(?:[/?]|$)", re.IGNORECASE)
_ASIN_STRICT = re.compile(r"^[A-Z0-9]{10}$")
_RATING_RE = re.compile(r"(\d+[.,]\d+|\d+)\s*(?:van de|van|out of|/)\s*5")
_INT_RE = re.compile("\\d[\\d.\u00a0\u202f ]*")
_PRICE_RE = re.compile("(\\d{1,3}(?:[.\u00a0\u202f ]\\d{3})*(?:,\\d{1,2})?|\\d+(?:\\.\\d{1,2})?)")


# -- low-level helpers ------------------------------------------------------


async def first_text(scope: Page | Locator, candidates: list[str]) -> str | None:
    """Text content of the first candidate selector that matches, stripped."""
    for selector in candidates:
        try:
            locator = scope.locator(selector).first
            if await locator.count() == 0:
                continue
            text = await locator.text_content(timeout=1_500)
        except PlaywrightError:
            continue
        if text and text.strip():
            return _clean(text)
    return None


async def first_attr(scope: Page | Locator, candidates: list[str], attr: str) -> str | None:
    """Attribute of the first candidate selector that matches and has it set."""
    for selector in candidates:
        try:
            locator = scope.locator(selector).first
            if await locator.count() == 0:
                continue
            value = await locator.get_attribute(attr, timeout=1_500)
        except PlaywrightError:
            continue
        if value and value.strip():
            return value.strip()
    return None


async def exists(scope: Page | Locator, candidates: list[str]) -> bool:
    """Whether any candidate selector matches at least one element."""
    for selector in candidates:
        try:
            if await scope.locator(selector).count() > 0:
                return True
        except PlaywrightError:
            continue
    return False


async def first_locator(scope: Page | Locator, candidates: list[str]) -> Locator | None:
    """The first candidate selector that matches, as a Locator."""
    for selector in candidates:
        try:
            locator = scope.locator(selector)
            if await locator.count() > 0:
                return locator.first
        except PlaywrightError:
            continue
    return None


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("‎", "").replace("‏", "")).strip()


# -- scalar parsers (pure, unit-testable) -----------------------------------


def parse_price(display: str | None) -> Money | None:
    """Parse a rendered price such as ``'€ 24,99'`` or ``'EUR 1.299,00'``."""
    if not display:
        return None
    text = _clean(display)
    match = _PRICE_RE.search(text)
    if not match:
        return None
    raw = match.group(1)
    # Dutch formatting: '.' and NBSP group thousands, ',' is the decimal mark.
    normalised = re.sub(r"[\s\u00a0\u202f]", "", raw)
    if "," in normalised:
        normalised = normalised.replace(".", "").replace(",", ".")
    elif normalised.count(".") > 1:
        normalised = normalised.replace(".", "")
    try:
        amount = float(normalised)
    except ValueError:
        return None
    # amazon.nl quotes EUR exclusively; the symbol check exists to reject a
    # stray number that is not a price at all.
    if "€" not in text and "eur" not in text.lower():
        return None
    return Money(amount=amount, currency="EUR", display=text)


def parse_rating(text: str | None) -> float | None:
    """Parse ``'4,5 van 5 sterren'`` / ``'4.5 out of 5 stars'`` into ``4.5``."""
    if not text:
        return None
    match = _RATING_RE.search(_clean(text))
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", "."))
    except ValueError:
        return None
    return value if 0.0 <= value <= 5.0 else None


def parse_int(text: str | None) -> int | None:
    """Parse the first integer out of text like ``'1.234 beoordelingen'``."""
    if not text:
        return None
    match = _INT_RE.search(_clean(text))
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def asin_from_url(url: str | None) -> str | None:
    """Pull the ASIN out of any amazon product URL shape."""
    if not url:
        return None
    match = _ASIN_RE.search(url)
    return match.group(1).upper() if match else None


def is_valid_asin(value: str | None) -> bool:
    return bool(value) and bool(_ASIN_STRICT.match(value or ""))


def product_url(base_url: str, asin: str) -> str:
    return f"{base_url}/dp/{asin}"


def _absolute(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("http"):
        return href
    return urljoin(base_url + "/", href.lstrip("/"))


def canonical_product_url(base_url: str, href: str | None, asin: str | None) -> str:
    """Prefer a clean ``/dp/<asin>`` URL; fall back to the raw href."""
    if asin:
        return product_url(base_url, asin)
    absolute = _absolute(base_url, href)
    if absolute:
        parsed = urlparse(absolute)
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return base_url


# -- composite extractors ---------------------------------------------------


async def extract_price(scope: Page | Locator) -> Money | None:
    """Read a price from a search row, preferring the accessible full string."""
    money = parse_price(await first_text(scope, S.RESULT_PRICE_DISPLAY))
    if money is not None:
        return money
    whole = await first_text(scope, S.RESULT_PRICE_WHOLE)
    fraction = await first_text(scope, S.RESULT_PRICE_FRACTION)
    if whole:
        return parse_price(f"€ {whole.rstrip(',.')},{(fraction or '00').lstrip(',.')}")
    return None


async def extract_search_row(row: Locator, base_url: str) -> ProductSummary | None:
    """Build a :class:`ProductSummary` from one search result row.

    Returns ``None`` for rows that are not products (ad slots, banner cards),
    which Amazon interleaves into the same list.
    """
    asin = (await row.get_attribute("data-asin") or "").strip().upper()
    href = await first_attr(row, S.RESULT_LINK, "href")
    if not is_valid_asin(asin):
        asin = asin_from_url(href) or ""
    if not is_valid_asin(asin):
        return None

    title = await first_text(row, S.RESULT_TITLE)
    if not title:
        return None

    return ProductSummary(
        asin=asin,
        title=title,
        url=canonical_product_url(base_url, href, asin),
        price=await extract_price(row),
        rating=parse_rating(
            await first_text(row, S.RESULT_RATING) or await first_attr(row, S.RESULT_RATING, "aria-label")
        ),
        review_count=parse_int(
            await first_text(row, S.RESULT_REVIEW_COUNT)
            or await first_attr(row, S.RESULT_REVIEW_COUNT, "aria-label")
        ),
        image_url=await first_attr(row, S.RESULT_IMAGE, "src"),
        is_prime=await exists(row, S.RESULT_PRIME),
        is_sponsored=await _is_sponsored(row),
        availability=await first_text(row, S.RESULT_AVAILABILITY),
    )


async def _is_sponsored(row: Locator) -> bool:
    """Paid placements advertise themselves four different ways; any one counts."""
    classes = await row.get_attribute("class") or ""
    if S.SPONSORED_CONTAINER_CLASS in classes.split():
        return True
    return await exists(row, S.RESULT_SPONSORED)


async def extract_search_rows(page: Page, base_url: str, limit: int) -> list[ProductSummary]:
    """Extract up to ``limit`` products from a search results page."""
    products: list[ProductSummary] = []
    seen: set[str] = set()
    # Every candidate selector is tried, not just the first that matches: Amazon
    # serves mixed layouts on one page often enough that stopping early silently
    # drops rows. The ASIN set keeps the overlap from double-counting.
    for selector in S.SEARCH_RESULT_ROW:
        rows = page.locator(selector)
        try:
            count = await rows.count()
        except PlaywrightError:
            continue
        for index in range(count):
            if len(products) >= limit:
                return products
            product = await extract_search_row(rows.nth(index), base_url)
            if product is None or product.asin in seen:
                continue
            seen.add(product.asin)
            products.append(product)
    return products


async def extract_variants(page: Page) -> list[ProductVariant]:
    """Read the twister (size/colour/pack) children of a parent listing."""
    variants: list[ProductVariant] = []
    seen: set[str] = set()
    # Every candidate selector, not just the first that matches: a listing that
    # varies on two axes (size *and* colour) puts each axis under its own
    # container, and stopping early would drop all but one of them.
    for selector in S.PDP_VARIANT_ITEMS:
        items = page.locator(selector)
        count = await items.count()
        for index in range(min(count, 40)):
            item = items.nth(index)
            asin = (
                (
                    await item.get_attribute("data-defaultasin")
                    or await item.get_attribute("data-asin")
                    or asin_from_url(await item.get_attribute("data-dp-url"))
                    or ""
                )
                .strip()
                .upper()
            )
            if not is_valid_asin(asin) or asin in seen:
                continue
            label = _clean(await item.inner_text()) or asin
            variants.append(
                ProductVariant(asin=asin, label=label[:120], dimension=await _dimension_of(item, selector))
            )
            seen.add(asin)
        if variants:
            break
    return variants


_VARIATION_ID = re.compile(r"variation_(.+?)_name", re.IGNORECASE)


async def _dimension_of(item: Locator, selector: str) -> str | None:
    """Name the variation axis this child belongs to.

    Amazon wraps each axis in a container whose id is ``variation_<axis>_name``,
    so the container is asked first; the selector that matched is the fallback
    for the shapes that carry the axis in the selector itself.
    """
    try:
        container_id = await item.evaluate(
            "el => el.closest('[id^=\"variation_\"]')?.id ?? null", timeout=1_500
        )
    except PlaywrightError:
        container_id = None
    if isinstance(container_id, str):
        match = _VARIATION_ID.search(container_id)
        if match:
            return match.group(1).lower()
    match = _VARIATION_ID.search(selector)
    return match.group(1).lower() if match else None


async def extract_cart_line(line: Locator, base_url: str) -> CartLine | None:
    """Build a :class:`CartLine` from one cart row."""
    asin = (await line.get_attribute("data-asin") or "").strip().upper()
    title = await first_text(line, S.CART_LINE_TITLE)
    if not title:
        return None

    quantity = await _cart_line_quantity(line)
    price = parse_price(await first_text(line, S.CART_LINE_PRICE))
    line_total: Money | None = None
    if price is not None and price.amount is not None and quantity > 1:
        line_total = Money(
            amount=round(price.amount * quantity, 2),
            currency=price.currency,
            display=None,
        )
    href = await first_attr(line, S.CART_LINE_LINK, "href")
    return CartLine(
        asin=asin if is_valid_asin(asin) else asin_from_url(href),
        title=title,
        quantity=quantity,
        price=price,
        line_total=line_total,
        availability=await first_text(line, S.CART_LINE_AVAILABILITY),
        url=canonical_product_url(base_url, href, asin if is_valid_asin(asin) else None),
    )


async def _cart_line_quantity(line: Locator) -> int:
    """Quantity of a cart line, whichever of Amazon's three widgets renders it."""
    box = await first_locator(line, S.CART_LINE_QUANTITY_INPUT)
    if box is not None:
        try:
            parsed = parse_int(await box.input_value(timeout=1_500))
            if parsed:
                return parsed
        except PlaywrightError:
            pass
        parsed = parse_int(await box.get_attribute("value"))
        if parsed:
            return parsed

    select = await first_locator(line, S.CART_LINE_QUANTITY_SELECT)
    if select is not None:
        try:
            value = await select.input_value(timeout=1_500)
            parsed = parse_int(value)
            if parsed:
                return parsed
        except PlaywrightError:
            pass
    text = await first_text(line, S.CART_LINE_QUANTITY_TEXT)
    parsed = parse_int(text)
    if parsed:
        return parsed
    return 1


async def extract_cart_lines(page: Page, base_url: str) -> list[CartLine]:
    """Extract every active line item from the cart page."""
    lines: list[CartLine] = []
    for selector in S.CART_LINE:
        rows = page.locator(selector)
        count = await rows.count()
        if count == 0:
            continue
        for index in range(count):
            line = await extract_cart_line(rows.nth(index), base_url)
            if line is not None:
                lines.append(line)
        if lines:
            break
    return lines


async def read_cart_count(page: Page) -> int:
    """Units in the cart, per the nav badge, with the aria-label as a second read."""
    count = parse_int(await first_text(page, S.NAV_CART_COUNT))
    if count is not None:
        return count
    return parse_int(await first_attr(page, S.NAV_CART_ARIA, "aria-label")) or 0


async def page_text_lower(page: Page, limit: int = 20_000) -> str:
    """Lower-cased visible body text, for marker matching. Never logged."""
    try:
        text = await page.inner_text("body", timeout=5_000)
    except PlaywrightError:
        return ""
    return text[:limit].lower()
