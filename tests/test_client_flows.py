"""End-to-end tests of :class:`AmazonClient` against the fake storefront.

Everything below the HTTP boundary is production code: navigation, consent
handling, bot-wall screening, extraction, and the add-to-cart verification.
"""

from __future__ import annotations

import pytest

from amazon_nl_mcp.amazon.client import AmazonClient
from amazon_nl_mcp.errors import (
    BotWallError,
    CartVerificationError,
    NetworkError,
    NotLoggedInError,
    ProductNotFoundError,
    ProductUnavailableError,
    VariantSelectionRequiredError,
)

from .fake_amazon import FakeAmazon


class TestSearch:
    async def test_returns_organic_results_only_by_default(self, amazon_client: AmazonClient) -> None:
        results = await amazon_client.search_products("usb c kabel", limit=10)
        assert results.query == "usb c kabel"
        assert results.count == len(results.products)
        assert all(not p.is_sponsored for p in results.products)
        assert "B0CX23V2ZK" in {p.asin for p in results.products}
        assert results.has_more is True
        assert results.next_page == 2

    async def test_sponsored_rows_can_be_asked_for(self, amazon_client: AmazonClient) -> None:
        results = await amazon_client.search_products("usb c kabel", include_sponsored=True)
        assert any(p.is_sponsored for p in results.products)

    async def test_limit_is_applied_after_filtering(self, amazon_client: AmazonClient) -> None:
        results = await amazon_client.search_products("usb c kabel", limit=2)
        assert len(results.products) == 2
        assert all(not p.is_sponsored for p in results.products)

    async def test_no_results_is_not_an_error(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        fake_amazon.route(r"/s\?", "search_no_results.html")
        results = await amazon_client.search_products("qwertyuiopasdfgh")
        assert results.count == 0
        assert results.has_more is False

    async def test_a_bot_wall_trips_the_breaker(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        fake_amazon.route(r"/s\?", "bot_wall.html")
        with pytest.raises(BotWallError):
            await amazon_client.search_products("usb c kabel")
        # And the next call is refused without touching the network at all.
        before = len(fake_amazon.requests)
        with pytest.raises(BotWallError):
            await amazon_client.search_products("usb c kabel")
        assert len(fake_amazon.requests) == before

    async def test_search_on_a_signed_out_session_says_so(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        fake_amazon.route(r"/s\?", "signed_out.html")
        with pytest.raises(NotLoggedInError):
            await amazon_client.search_products("usb c kabel")


class TestProduct:
    async def test_reads_a_product_page(self, amazon_client: AmazonClient) -> None:
        product = await amazon_client.get_product("B0CX23V2ZK")
        assert product.asin == "B0CX23V2ZK"
        assert product.title.startswith("Anker USB-C")
        assert product.in_stock is True
        assert product.price is not None
        assert product.price.amount == pytest.approx(18.99)
        assert product.brand == "Merk: Anker"
        assert len(product.bullets) == 2
        assert product.requires_variant_selection is False

    async def test_unknown_asin_is_reported_as_not_found(self, amazon_client: AmazonClient) -> None:
        with pytest.raises(ProductNotFoundError):
            await amazon_client.get_product("B0MISSING1")

    async def test_variant_parent_is_flagged_with_its_children(self, amazon_client: AmazonClient) -> None:
        product = await amazon_client.get_product("B0PARENT01")
        assert product.requires_variant_selection is True
        assert product.in_stock is False
        assert [v.asin for v in product.variants] == ["B0CHILD042", "B0CHILD043"]

    async def test_out_of_stock_listing(self, amazon_client: AmazonClient) -> None:
        product = await amazon_client.get_product("B0GONE0001")
        assert product.in_stock is False
        assert product.availability is not None


class TestCart:
    async def test_reads_lines_and_subtotal(self, amazon_client: AmazonClient) -> None:
        cart = await amazon_client.view_cart()
        assert cart.line_count == 1
        assert cart.item_count == 1
        assert cart.lines[0].asin == "B07LEGACY1"
        assert cart.subtotal is not None
        assert cart.subtotal.amount == pytest.approx(8.49)
        assert cart.cart_url.endswith("/gp/cart/view.html")

    async def test_empty_cart(self, amazon_client: AmazonClient, fake_amazon: FakeAmazon) -> None:
        fake_amazon.cart_before = "cart_empty.html"
        cart = await amazon_client.view_cart()
        assert cart.line_count == 0
        assert cart.item_count == 0

    async def test_signed_out_cart_raises(self, amazon_client: AmazonClient, fake_amazon: FakeAmazon) -> None:
        fake_amazon.cart_before = "signed_out.html"
        with pytest.raises(NotLoggedInError):
            await amazon_client.view_cart()


class TestAddToCart:
    async def test_happy_path_uses_the_add_url_and_verifies_the_cart(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        result = await amazon_client.add_to_cart("B0CX23V2ZK", quantity=2)

        assert result.ok is True
        assert result.asin == "B0CX23V2ZK"
        assert result.requested_quantity == 2
        assert result.quantity_in_cart == 2
        assert result.cart_item_count == 3
        assert fake_amazon.add_hits == 1, "the legacy add endpoint is the primary mechanism"
        assert "add_url" in result.message
        assert "never places orders" in result.message

    async def test_falls_back_to_the_product_page_when_the_add_url_does_nothing(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        # The endpoint answers with its usual confirmation but the cart never
        # changes — exactly the failure the banner would hide.
        fake_amazon.add_succeeds = False
        with pytest.raises(CartVerificationError):
            await amazon_client.add_to_cart("B0CX23V2ZK")
        assert fake_amazon.add_hits >= 1

    async def test_a_success_banner_alone_is_never_enough(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        fake_amazon.add_succeeds = False
        with pytest.raises(CartVerificationError) as excinfo:
            await amazon_client.add_to_cart("B0CX23V2ZK")
        assert "cart" in str(excinfo.value).lower()

    async def test_variant_parent_is_refused_before_any_write(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        with pytest.raises(VariantSelectionRequiredError) as excinfo:
            await amazon_client.add_to_cart("B0PARENT01")
        assert fake_amazon.add_hits == 0, "nothing may be sent for a listing that needs a variant"
        assert "B0CHILD042" in str(excinfo.value) or "Maat 42" in str(excinfo.value)

    async def test_out_of_stock_is_refused_before_any_write(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        with pytest.raises(ProductUnavailableError):
            await amazon_client.add_to_cart("B0GONE0001")
        assert fake_amazon.add_hits == 0

    async def test_the_variant_interstitial_from_the_add_endpoint_is_caught(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        fake_amazon.add_response = "variant_required_interstitial.html"
        fake_amazon.add_succeeds = False
        with pytest.raises(VariantSelectionRequiredError):
            await amazon_client.add_to_cart("B0CX23V2ZK")


class TestSessionStatus:
    async def test_authenticated(self, amazon_client: AmazonClient) -> None:
        status = await amazon_client.session_status()
        assert status.state == "authenticated"
        assert status.signed_in is True
        assert status.account_label is None, "the account name is withheld unless asked for"

    async def test_reveals_the_account_only_on_request(self, amazon_client: AmazonClient) -> None:
        status = await amazon_client.session_status(reveal_account=True)
        assert status.account_label == "Hallo, Chris"

    async def test_signed_out(self, amazon_client: AmazonClient, fake_amazon: FakeAmazon) -> None:
        fake_amazon.route(r"amazon\.nl/?$", "signed_out.html")
        status = await amazon_client.session_status()
        assert status.state == "signed_out"
        assert "login" in status.detail

    async def test_a_network_failure_raises_the_right_error_from_a_tool(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        fake_amazon.offline = True
        with pytest.raises(NetworkError):
            await amazon_client.search_products("usb c kabel")

    async def test_blocked_never_raises(self, amazon_client: AmazonClient, fake_amazon: FakeAmazon) -> None:
        fake_amazon.route(r"amazon\.nl/?$", "bot_wall.html")
        status = await amazon_client.session_status()
        assert status.state == "blocked"
        assert status.signed_in is False

    async def test_a_network_failure_is_not_reported_as_a_dead_browser(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        fake_amazon.offline = True
        status = await amazon_client.session_status()
        assert status.state == "unreachable"
        assert status.signed_in is False
        assert "connectivity" in status.detail


class TestDoubleAddSafety:
    async def test_a_stale_cart_read_does_not_cause_a_second_add(
        self, amazon_client: AmazonClient, fake_amazon: FakeAmazon
    ) -> None:
        """The cart is briefly stale after an add; that must not trigger the fallback.

        Treating the first stale read as failure would send the product-page
        mechanism at an item that is already in the cart, adding it twice.
        """
        real_fixture_for = fake_amazon._fixture_for
        stale_reads = {"remaining": 1}

        def fixture_for(url: str) -> str | None:
            if "/gp/cart/view.html" in url and fake_amazon.add_hits and stale_reads["remaining"]:
                stale_reads["remaining"] -= 1
                return fake_amazon.cart_before  # the pre-add cart, served once too often
            return real_fixture_for(url)

        fake_amazon._fixture_for = fixture_for  # type: ignore[method-assign]

        result = await amazon_client.add_to_cart("B0CX23V2ZK", quantity=2)

        assert result.ok is True
        assert result.quantity_in_cart == 2
        assert fake_amazon.add_hits == 1, "the add endpoint must not be hit twice"
        assert "add_url" in result.message, "the fallback must not have been used"
