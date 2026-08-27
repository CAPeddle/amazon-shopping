"""The MCP tool surface, exercised through the SDK's in-memory client.

No transport and no browser: these tests are about the contract the model sees —
tool names, schemas, annotations, and what a failure looks like on the wire.
"""

from __future__ import annotations

import pytest
from mcp import Client
from mcp.server import MCPServer

from amazon_nl_mcp.amazon.client import AmazonClient
from amazon_nl_mcp.config import Settings
from amazon_nl_mcp.models import Cart, ProductDetail, SearchResults, SessionStatus
from amazon_nl_mcp.server import create_mcp_server

EXPECTED_TOOLS = {
    "amazon_search_products",
    "amazon_get_product",
    "amazon_add_to_cart",
    "amazon_view_cart",
    "amazon_session_status",
}


@pytest.fixture
def mcp(amazon_client: AmazonClient, settings: Settings) -> MCPServer:
    return create_mcp_server(amazon_client, settings)


async def test_the_surface_is_exactly_the_five_tools(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        assert {tool.name for tool in (await client.list_tools()).tools} == EXPECTED_TOOLS


async def test_annotations_mark_the_one_writing_tool(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    for name in EXPECTED_TOOLS - {"amazon_add_to_cart"}:
        annotations = tools[name].annotations
        assert annotations is not None, name
        assert annotations.read_only_hint is True, name

    add = tools["amazon_add_to_cart"].annotations
    assert add is not None
    assert add.read_only_hint is False
    assert add.destructive_hint is False, "filling a cart removes nothing"


async def test_every_tool_publishes_an_output_schema(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        for tool in (await client.list_tools()).tools:
            assert tool.output_schema is not None, tool.name
            assert tool.description, f"{tool.name} has no description for the model to read"


async def test_search_round_trips_through_the_model(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool("amazon_search_products", {"query": "usb c kabel", "limit": 3})
    assert result.is_error is False
    parsed = SearchResults.model_validate(result.structured_content)
    assert parsed.count <= 3
    assert all(not p.is_sponsored for p in parsed.products)


async def test_get_product_and_view_cart_round_trip(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        product = await client.call_tool("amazon_get_product", {"asin": "B0CX23V2ZK"})
        cart = await client.call_tool("amazon_view_cart", {})
    assert ProductDetail.model_validate(product.structured_content).asin == "B0CX23V2ZK"
    assert Cart.model_validate(cart.structured_content).line_count == 1


async def test_session_status_never_reveals_the_account_by_default(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool("amazon_session_status", {})
    status = SessionStatus.model_validate(result.structured_content)
    assert status.state == "authenticated"
    assert status.account_label is None


async def test_a_bad_asin_is_rejected_by_the_schema(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool("amazon_get_product", {"asin": "short"})
    assert result.is_error is True


async def test_an_expected_failure_reaches_the_model_with_its_remedy(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool("amazon_get_product", {"asin": "B0MISSING1"})
    assert result.is_error is True
    text = result.content[0].text  # type: ignore[union-attr]
    assert "B0MISSING1" in text
    assert "amazon_search_products" in text, "the error must name the way out"


async def test_a_read_only_deployment_does_not_advertise_the_write_tool(
    amazon_client: AmazonClient, settings: Settings
) -> None:
    """A tool the model cannot see beats one that always refuses."""
    read_only = settings.model_copy(update={"write_enabled": False})
    async with Client(create_mcp_server(amazon_client, read_only), raise_exceptions=True) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert names == EXPECTED_TOOLS - {"amazon_add_to_cart"}
        result = await client.call_tool("amazon_add_to_cart", {"asin": "B0CX23V2ZK"})
    assert result.is_error is True


async def test_add_to_cart_reports_what_it_verified(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        result = await client.call_tool("amazon_add_to_cart", {"asin": "B0CX23V2ZK", "quantity": 2})
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["quantity_in_cart"] == 2
    assert result.structured_content["ok"] is True


async def test_instructions_tell_the_model_where_the_boundary_is(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        assert client.instructions is not None
        assert "never places an order" in client.instructions


async def test_the_pagination_contract_is_self_consistent(mcp: MCPServer) -> None:
    """A truncated page must not hand back a next_page that would skip rows."""
    async with Client(mcp, raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        limit_schema = tools["amazon_search_products"].input_schema["properties"]["limit"]

        truncated = SearchResults.model_validate(
            (
                await client.call_tool("amazon_search_products", {"query": "usb c kabel", "limit": 1})
            ).structured_content
        )
        assert truncated.truncated is True
        assert truncated.next_page is None

        # The remedy the description gives has to be reachable within the schema.
        assert limit_schema["maximum"] >= 60

        full = SearchResults.model_validate(
            (
                await client.call_tool("amazon_search_products", {"query": "usb c kabel", "limit": 60})
            ).structured_content
        )
        assert full.truncated is False
        assert full.next_page == 2


async def test_a_next_page_value_is_always_accepted_by_the_schema(mcp: MCPServer) -> None:
    async with Client(mcp, raise_exceptions=True) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        page_max = tools["amazon_search_products"].input_schema["properties"]["page"]["maximum"]
        result = await client.call_tool(
            "amazon_search_products", {"query": "usb c kabel", "limit": 60, "page": page_max}
        )
    parsed = SearchResults.model_validate(result.structured_content)
    assert parsed.next_page is None, "the last allowed page must not point past the schema"
