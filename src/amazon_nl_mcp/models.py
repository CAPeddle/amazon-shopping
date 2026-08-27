"""Wire models.

These are the tool return types, so each one is simultaneously the MCP
``output_schema`` and the REST/JSON shape. Fields are deliberately flat and
optional-tolerant: amazon.nl A/B tests its markup, and a missing price should
degrade one field rather than fail the whole call.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SessionState = Literal["authenticated", "signed_out", "blocked", "unreachable", "browser_down", "unknown"]


class Money(BaseModel):
    """A price as shown on the storefront."""

    amount: float | None = Field(default=None, description="Numeric value, e.g. 24.99. None if unreadable.")
    currency: str = Field(default="EUR", description="ISO currency code; amazon.nl quotes EUR.")
    display: str | None = Field(default=None, description="Exactly as rendered, e.g. '€ 24,99'.")


class ProductSummary(BaseModel):
    """One row of a search results page."""

    asin: str = Field(description="Amazon Standard Identification Number, the stable product id.")
    title: str = Field(description="Product title as listed.")
    url: str = Field(description="Canonical amazon.nl product URL.")
    price: Money | None = Field(default=None, description="Current price, when the listing shows one.")
    rating: float | None = Field(default=None, description="Average star rating out of 5.")
    review_count: int | None = Field(default=None, description="Number of customer ratings.")
    image_url: str | None = Field(default=None, description="Thumbnail image URL.")
    is_prime: bool = Field(default=False, description="Whether the listing carries a Prime badge.")
    is_sponsored: bool = Field(default=False, description="Whether this row is a paid placement.")
    availability: str | None = Field(default=None, description="Availability text when the row shows one.")


class SearchResults(BaseModel):
    """Return type of ``amazon_search_products``."""

    query: str = Field(description="The query that was searched.")
    count: int = Field(description="Number of products in this response.")
    page: int = Field(description="1-based results page that was read.")
    has_more: bool = Field(
        description="Whether more results exist, either further down this page or on the next one."
    )
    truncated: bool = Field(
        default=False,
        description=(
            "True when this page held more matches than `limit` allowed through. Raise `limit` to "
            "see the rest of THIS page — going to next_page would skip them."
        ),
    )
    next_page: int | None = Field(
        default=None,
        description=("The next amazon.nl results page, or None when this one was truncated or was the last."),
    )
    results_url: str = Field(description="The search URL that produced these rows.")
    products: list[ProductSummary] = Field(description="The matching products, in Amazon's own order.")


class ProductVariant(BaseModel):
    """A selectable child of a parent listing (a size, a colour, a pack count)."""

    asin: str = Field(description="Child ASIN to add to the cart.")
    label: str = Field(description="Human label of the variation, e.g. 'Maat 42' or 'Zwart'.")
    dimension: str | None = Field(default=None, description="Which axis this varies on, when known.")


class ProductDetail(BaseModel):
    """Return type of ``amazon_get_product``."""

    asin: str = Field(description="The product's ASIN.")
    title: str = Field(description="Product title.")
    url: str = Field(description="Canonical amazon.nl product URL.")
    price: Money | None = Field(default=None, description="Buy-box price, when one is shown.")
    availability: str | None = Field(default=None, description="Availability line, e.g. 'Op voorraad'.")
    in_stock: bool = Field(description="Whether an add-to-cart control is present and enabled.")
    rating: float | None = Field(default=None, description="Average star rating out of 5.")
    review_count: int | None = Field(default=None, description="Number of customer ratings.")
    image_url: str | None = Field(default=None, description="Main product image URL.")
    brand: str | None = Field(default=None, description="Brand or 'by-line' text.")
    bullets: list[str] = Field(default_factory=list, description="Feature bullets from the listing.")
    variants: list[ProductVariant] = Field(
        default_factory=list,
        description="Child listings to choose between. Non-empty means this ASIN itself may not be addable.",
    )
    requires_variant_selection: bool = Field(
        default=False, description="True when a variant must be picked before adding to the cart."
    )


class CartLine(BaseModel):
    """One line item in the cart."""

    asin: str | None = Field(default=None, description="ASIN of the line item, when readable.")
    title: str = Field(description="Product title as shown in the cart.")
    quantity: int = Field(description="Quantity currently in the cart for this line.")
    price: Money | None = Field(default=None, description="Unit price as shown.")
    line_total: Money | None = Field(default=None, description="Price times quantity, when shown.")
    availability: str | None = Field(default=None, description="Any stock note on the line.")
    url: str | None = Field(default=None, description="Product URL for the line item.")


class Cart(BaseModel):
    """Return type of ``amazon_view_cart``."""

    item_count: int = Field(description="Total units in the cart, per Amazon's own counter.")
    line_count: int = Field(description="Number of distinct line items.")
    subtotal: Money | None = Field(default=None, description="Cart subtotal as shown.")
    lines: list[CartLine] = Field(default_factory=list, description="The cart's line items.")
    cart_url: str = Field(description="URL of the cart page, to open in a browser and check out by hand.")


class AddToCartResult(BaseModel):
    """Return type of ``amazon_add_to_cart``."""

    ok: bool = Field(description="Whether the item was verified present in the cart afterwards.")
    asin: str = Field(description="ASIN that was added.")
    title: str | None = Field(default=None, description="Title of the added product, when known.")
    requested_quantity: int = Field(description="Quantity that was requested.")
    quantity_in_cart: int = Field(description="Quantity of this ASIN in the cart after the add.")
    price: Money | None = Field(default=None, description="Unit price at the time of adding.")
    cart_item_count: int = Field(description="Total units in the cart after the add.")
    cart_url: str = Field(description="Cart URL, to review and check out manually.")
    message: str = Field(description="Human-readable summary of what happened.")


class SessionStatus(BaseModel):
    """Return type of ``amazon_session_status``."""

    state: SessionState = Field(
        description=(
            "'authenticated' = signed in and usable; 'signed_out' = re-login needed; "
            "'blocked' = Amazon is serving an automation check; 'unreachable' = amazon.nl could "
            "not be reached from this host; 'browser_down' = Chromium is not running; "
            "'unknown' = could not be determined."
        )
    )
    signed_in: bool = Field(description="Convenience flag: state == 'authenticated'.")
    account_label: str | None = Field(
        default=None,
        description="Nav greeting, only when the caller explicitly asked to reveal it. Normally None.",
    )
    cart_item_count: int | None = Field(default=None, description="Units in the cart, when readable.")
    storefront: str = Field(description="Storefront this service talks to.")
    profile_dir: str = Field(description="Browser profile directory backing the session.")
    write_enabled: bool = Field(description="Whether cart mutations are permitted on this deployment.")
    detail: str = Field(description="What to do next, in one sentence.")
