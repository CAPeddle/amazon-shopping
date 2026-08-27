import json
import pytest
from mcp import Client
from amazon_nl_mcp.server import create_mcp_server

async def test_probe_cart(amazon_client, settings):
    mcp = create_mcp_server(amazon_client, settings)
    async with Client(mcp, raise_exceptions=True) as c:
        r = await c.call_tool("amazon_view_cart", {})
        print("CART:", json.dumps(r.structured_content, indent=2, ensure_ascii=False))
        s = await c.call_tool("amazon_search_products", {"query":"usb c kabel","limit":2})
        print("SEARCH:", json.dumps({k:v for k,v in s.structured_content.items() if k!="products"}, indent=2))
        print("N products:", len(s.structured_content["products"]))
        s2 = await c.call_tool("amazon_search_products", {"query":"usb c kabel","limit":50,"include_sponsored":True})
        print("SEARCH ALL:", json.dumps({k:v for k,v in s2.structured_content.items() if k!="products"}, indent=2))
        print("all asins:", [p["asin"] for p in s2.structured_content["products"]])
