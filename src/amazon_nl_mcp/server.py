"""The MCP tool surface.

Five tools, one job each, named with an ``amazon_`` prefix so they stay
unambiguous next to whatever other MCP servers a client has loaded.

The shopping boundary is deliberate and enforced here rather than trusted to the
caller: this server can search, inspect, add to the cart, and read the cart. It
cannot change quantities, remove lines, or place an order. Checkout stays a
human action in a real browser.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from amazon_nl_mcp.amazon.client import MAX_ROWS_PER_PAGE, MAX_SEARCH_PAGE, AmazonClient
from amazon_nl_mcp.config import Settings
from amazon_nl_mcp.errors import AmazonMCPError
from amazon_nl_mcp.logging import get_logger
from amazon_nl_mcp.models import (
    AddToCartResult,
    Cart,
    ProductDetail,
    SearchResults,
    SessionStatus,
)

log = get_logger(__name__)

INSTRUCTIONS = """\
Search amazon.nl and manage the owner's personal Amazon shopping cart.

Call amazon_session_status first if anything fails: every tool here depends on a
browser profile that is signed in to amazon.nl, and an expired login is the most
common cause of failure.

Typical flow: amazon_search_products to find candidates, amazon_get_product for
the details of one ASIN, amazon_add_to_cart to put it in the cart, and
amazon_view_cart to confirm. The ASIN is the identifier that ties them together.

This server never places an order. Adding to the cart is as far as it goes; the
owner reviews the cart and checks out in their own browser.\
"""

_ASIN_FIELD = Field(
    description="Amazon Standard Identification Number, the 10-character product id (e.g. 'B0CX23V2ZK').",
    min_length=10,
    max_length=10,
    pattern=r"^[A-Za-z0-9]{10}$",
)


def create_mcp_server(client: AmazonClient, settings: Settings) -> MCPServer:
    """Build the MCP server, with every tool bound to ``client``."""
    mcp: MCPServer = MCPServer(
        "amazon_nl_mcp",
        title="amazon.nl shopping",
        version="0.1.0",
        instructions=INSTRUCTIONS,
        log_level=settings.log_level.upper(),  # type: ignore[arg-type]
    )

    async def _guard(coro: object, *, tool: str) -> object:
        """Run a client call under the tool timeout, mapping failures for the model."""
        try:
            return await asyncio.wait_for(coro, timeout=settings.tool_timeout_s)  # type: ignore[arg-type]
        except TimeoutError as exc:
            raise ToolError(
                f"{tool} exceeded {settings.tool_timeout_s:.0f}s against amazon.nl and was cancelled. "
                "Retry; if it keeps timing out, check amazon_session_status."
            ) from exc
        except AmazonMCPError as exc:
            log.info("tool_failed", tool=tool, code=exc.code)
            raise ToolError(exc.as_text()) from exc

    @mcp.tool(
        title="Search amazon.nl",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def amazon_search_products(
        query: Annotated[
            str,
            Field(
                description="What to search for, in Dutch or English (e.g. 'usb c kabel 2m', 'espresso beans').",
                min_length=1,
                max_length=200,
            ),
        ],
        limit: Annotated[
            int,
            Field(
                description=(
                    "Maximum products to return from this page. If the result comes back with "
                    "truncated=true, raise this to see the rest of the page — going to next_page "
                    "would skip them."
                ),
                ge=1,
                le=MAX_ROWS_PER_PAGE,
            ),
        ] = 10,
        page: Annotated[
            int,
            Field(
                description=(
                    "1-based results page. Only ever pass a next_page value a previous call returned."
                ),
                ge=1,
                le=MAX_SEARCH_PAGE,
            ),
        ] = 1,
        include_sponsored: Annotated[
            bool,
            Field(description="Include paid placements. Off by default, since they crowd out real matches."),
        ] = False,
    ) -> SearchResults:
        """Search the amazon.nl storefront and return matching products.

        Returns each product's ASIN, title, price, rating and availability. The
        ASIN is what amazon_get_product and amazon_add_to_cart take. Sponsored
        rows are filtered out unless asked for. Results are amazon.nl's own
        ranking for the query, not a re-ranking.

        Reading the pagination fields: `truncated` means this page held more
        matches than `limit` allowed through — raise `limit` to see them.
        `next_page` is only ever set when you have seen the current page out, so
        following it never skips anything.
        """
        return await _guard(  # type: ignore[return-value]
            client.search_products(query, limit=limit, page_number=page, include_sponsored=include_sponsored),
            tool="amazon_search_products",
        )

    @mcp.tool(
        title="Get an amazon.nl product",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def amazon_get_product(
        asin: Annotated[str, _ASIN_FIELD],
    ) -> ProductDetail:
        """Read one amazon.nl product page in full.

        Use this before adding anything non-trivial to the cart: it reports the
        buy-box price, stock state, feature bullets, and — for listings that come
        in sizes or colours — the child ASINs to choose between. A listing whose
        requires_variant_selection is true cannot be added directly; add one of
        its variants instead.

        Check `resolved_from`: amazon.nl sometimes redirects an ASIN to another
        product, and when it does, the returned `asin` is the one that was
        actually loaded — not the one you asked for.
        """
        return await _guard(client.get_product(asin), tool="amazon_get_product")  # type: ignore[return-value]

    write_tool = mcp.tool(
        title="Add to the amazon.nl cart",
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
        ),
    )

    async def amazon_add_to_cart(
        asin: Annotated[str, _ASIN_FIELD],
        quantity: Annotated[
            int, Field(description="How many units to add to the existing cart contents.", ge=1, le=30)
        ] = 1,
    ) -> AddToCartResult:
        """Add a product to the owner's personal amazon.nl cart.

        This spends nothing: it only fills the cart, and the result is verified by
        re-reading the cart page rather than trusting Amazon's confirmation
        banner. The owner reviews the cart and checks out themselves — this
        server has no way to place an order.

        Calling twice adds twice. Check amazon_view_cart first if you are unsure
        whether an earlier call landed.

        `ok` is true only when the whole requested quantity landed; a partial add
        returns false with `added_quantity` showing what did. Check
        `resolved_from` too — a non-null value means amazon.nl redirected the
        ASIN you asked for to the one that was added.
        """
        return await _guard(  # type: ignore[return-value]
            client.add_to_cart(asin, quantity=quantity), tool="amazon_add_to_cart"
        )

    @mcp.tool(
        title="View the amazon.nl cart",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def amazon_view_cart() -> Cart:
        """Read the owner's current amazon.nl cart: line items, quantities, subtotal.

        Use it to confirm an add landed, to total up a basket before telling the
        owner, or to see what is already in there before adding more.
        """
        return await _guard(client.view_cart(), tool="amazon_view_cart")  # type: ignore[return-value]

    @mcp.tool(
        title="Check the amazon.nl session",
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=True),
    )
    async def amazon_session_status(
        reveal_account: Annotated[
            bool,
            Field(
                description=(
                    "Include the account greeting from the nav bar. Off by default so the owner's "
                    "name does not end up in a transcript."
                )
            ),
        ] = False,
    ) -> SessionStatus:
        """Report whether this server can currently act on amazon.nl.

        Call it first whenever another tool fails. It distinguishes the four
        failures that look alike from the outside: a signed-out profile
        (re-login needed), an automation check from Amazon (wait it out), a dead
        browser, and a working session. It never raises.
        """
        return await client.session_status(reveal_account=reveal_account)

    # A read-only deployment does not advertise a tool it would always refuse:
    # a tool the model cannot see is clearer than one that always errors, and it
    # keeps the surface honest about what this instance can do.
    if settings.write_enabled:
        write_tool(amazon_add_to_cart)

    # Referenced so linters see the decorated functions as used; the decorator
    # is what actually registers them with the server.
    _ = (
        amazon_search_products,
        amazon_get_product,
        amazon_view_cart,
        amazon_session_status,
    )
    return mcp
