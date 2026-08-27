"""Selector-table tests against saved amazon.nl HTML in a real Chromium.

These are the tests that fail when Amazon changes a layout: re-capture the page
into ``tests/fixtures/`` and whichever assertion breaks names the selector that
went stale.
"""

from __future__ import annotations

import pytest

from amazon_nl_mcp.amazon import extract as X
from amazon_nl_mcp.amazon import selectors as S

from .conftest import PageLoader

BASE = "https://www.amazon.nl"


class TestSearchResults:
    async def test_skips_ad_slots_with_empty_asin(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("search_results.html")
        products = await X.extract_search_rows(page, BASE, limit=20)
        assert all(len(p.asin) == 10 for p in products)
        assert "Banner" not in [p.title for p in products]

    async def test_reads_the_primary_row_completely(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("search_results.html")
        products = await X.extract_search_rows(page, BASE, limit=20)
        anker = next(p for p in products if p.asin == "B0CX23V2ZK")

        assert anker.title == "Anker USB-C naar USB-C kabel 2 m, 100 W"
        assert anker.url == f"{BASE}/dp/B0CX23V2ZK"
        assert anker.price is not None
        assert anker.price.amount == pytest.approx(18.99), "must read the live price, not the struck-out one"
        assert anker.rating == pytest.approx(4.4)
        assert anker.review_count == 1284
        assert anker.image_url == "https://m.media-amazon.com/images/I/anker.jpg"
        assert anker.is_prime is True
        assert anker.is_sponsored is False

    async def test_falls_back_to_whole_and_fraction_price(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("search_results.html")
        products = await X.extract_search_rows(page, BASE, limit=20)
        legacy = next(p for p in products if p.asin == "B07LEGACY1")

        assert legacy.title == "Ugreen USB-C kabel 1 m zwart"
        assert legacy.price is not None
        assert legacy.price.amount == pytest.approx(1299.00), (
            "thousands separator must not be a decimal point"
        )
        assert legacy.rating == pytest.approx(4.0)

    async def test_detects_sponsored_rows(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("search_results.html")
        products = await X.extract_search_rows(page, BASE, limit=20)
        sponsored = next(p for p in products if p.asin == "B09SPONSOR")
        assert sponsored.is_sponsored is True

    async def test_a_row_without_a_price_still_parses(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("search_results.html")
        products = await X.extract_search_rows(page, BASE, limit=20)
        unpriced = next(p for p in products if p.asin == "B08NOPRIC1")
        assert unpriced.price is None
        assert unpriced.title == "Onbekende kabel"

    async def test_limit_is_respected(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("search_results.html")
        assert len(await X.extract_search_rows(page, BASE, limit=2)) == 2

    async def test_next_page_link_is_found(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("search_results.html")
        assert await X.exists(page, S.SEARCH_NEXT_PAGE) is True

    async def test_no_results_page_yields_nothing(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("search_no_results.html")
        assert await X.extract_search_rows(page, BASE, limit=20) == []
        text = await X.page_text_lower(page)
        assert any(marker in text for marker in S.NO_RESULTS_MARKERS)


class TestProductDetail:
    async def test_core_fields(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("product_detail.html")
        assert await X.first_text(page, S.PDP_TITLE) == "Anker USB-C naar USB-C kabel 2 m, 100 W, zwart"
        price = X.parse_price(await X.first_text(page, S.PDP_PRICE_DISPLAY))
        assert price is not None
        assert price.amount == pytest.approx(18.99), "the strike-through list price must be excluded"
        assert await X.first_text(page, S.PDP_AVAILABILITY) == "Op voorraad"
        assert await X.exists(page, S.PDP_ADD_TO_CART) is True
        assert await X.first_attr(page, S.PDP_ASIN_INPUT, "value") == "B0CX23V2ZK"
        assert X.parse_rating(await X.first_text(page, S.PDP_RATING)) == pytest.approx(4.4)
        assert X.parse_int(await X.first_text(page, S.PDP_REVIEW_COUNT)) == 1284

    async def test_variant_parent_has_children_and_no_buy_button(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("product_variant_parent.html")
        assert await X.exists(page, S.PDP_ADD_TO_CART) is False
        variants = await X.extract_variants(page)
        assert [v.asin for v in variants] == ["B0CHILD042", "B0CHILD043"]
        assert variants[0].label == "Maat 42"
        assert variants[0].dimension == "size"

    async def test_out_of_stock_is_detected(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("product_out_of_stock.html")
        assert await X.exists(page, S.PDP_OUT_OF_STOCK) is True
        availability = await X.first_text(page, S.PDP_AVAILABILITY)
        assert availability is not None
        assert any(m in availability.lower() for m in S.UNAVAILABLE_TEXT_MARKERS)


class TestCart:
    async def test_reads_both_quantity_widgets(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("cart.html")
        lines = await X.extract_cart_lines(page, BASE)
        assert len(lines) == 2

        by_asin = {line.asin: line for line in lines}
        anker = by_asin["B0CX23V2ZK"]
        assert anker.quantity == 3, "quantityBox input"
        assert anker.price is not None
        assert anker.price.amount == pytest.approx(18.99)
        assert anker.line_total is not None
        assert anker.line_total.amount == pytest.approx(56.97)
        assert anker.url == f"{BASE}/dp/B0CX23V2ZK"

        ugreen = by_asin["B07LEGACY1"]
        assert ugreen.quantity == 1, "select[name=quantity]"
        assert ugreen.line_total is not None, "single units carry a line total too, so sums add up"
        assert ugreen.line_total.amount == pytest.approx(8.49)

    async def test_line_totals_sum_to_the_subtotal(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("cart.html")
        lines = await X.extract_cart_lines(page, BASE)
        subtotal = X.parse_price(await X.first_text(page, S.CART_SUBTOTAL))
        assert subtotal is not None
        summed = sum(
            line.line_total.amount
            for line in lines
            if line.line_total is not None and line.line_total.amount is not None
        )
        assert summed == pytest.approx(subtotal.amount)

    async def test_subtotal_and_count(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("cart.html")
        subtotal = X.parse_price(await X.first_text(page, S.CART_SUBTOTAL))
        assert subtotal is not None
        assert subtotal.amount == pytest.approx(65.46)
        assert X.parse_int(await X.first_text(page, S.CART_ITEM_COUNT_TEXT)) == 4
        assert await X.read_cart_count(page) == 4

    async def test_empty_cart(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("cart_empty.html")
        assert await X.extract_cart_lines(page, BASE) == []
        text = await X.page_text_lower(page)
        assert any(marker in text for marker in S.CART_EMPTY_MARKERS)


class TestSessionSignals:
    async def test_cart_count_falls_back_to_the_aria_label(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("search_results.html")
        assert await X.read_cart_count(page) == 3

    async def test_signed_out_page_carries_the_signin_role(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("signed_out.html")
        assert await X.exists(page, [S.NAV_SIGNIN_ROLE]) is True
        href = await X.first_attr(page, [S.NAV_ACCOUNT_LINK], "href")
        assert href is not None and "/ap/signin" in href

    async def test_signed_in_page_does_not(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("product_detail.html")
        assert await X.exists(page, [S.NAV_SIGNIN_ROLE]) is False
        greeting = await X.first_text(page, S.NAV_ACCOUNT_GREETING)
        assert greeting is not None
        assert greeting.lower() not in S.SIGNED_OUT_MARKERS

    async def test_bot_wall_markers(self, fixture_page: PageLoader) -> None:
        page = await fixture_page("bot_wall.html")
        assert await X.exists(page, S.CAPTCHA_MARKERS) is True
        text = await X.page_text_lower(page)
        assert any(marker in text for marker in S.CAPTCHA_TEXT_MARKERS)

    async def test_a_two_axis_twister_keeps_both_axes(self, fixture_page: PageLoader) -> None:
        """Stopping at the first matching selector would drop a whole axis."""
        page = await fixture_page("product_variant_two_axes.html")
        variants = await X.extract_variants(page)

        assert {v.asin for v in variants} == {"B0SIZE0036", "B0SIZE0038", "B0COLBLUE1", "B0COLBLAK1"}
        by_dimension: dict[str | None, set[str]] = {}
        for variant in variants:
            by_dimension.setdefault(variant.dimension, set()).add(variant.asin)
        assert by_dimension["size"] == {"B0SIZE0036", "B0SIZE0038"}
        assert by_dimension["color"] == {"B0COLBLUE1", "B0COLBLAK1"}

    async def test_axes_reachable_only_by_different_selectors_are_all_kept(
        self, fixture_page: PageLoader
    ) -> None:
        """One axis carries data-asin, the other only data-dp-url."""
        page = await fixture_page("product_variant_split_axes.html")
        variants = await X.extract_variants(page)

        assert {v.asin for v in variants} == {"B0SIZE0042", "B0COLBRWN1", "B0COLGREY1"}
        assert {v.dimension for v in variants} == {"size", "color"}
