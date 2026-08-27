"""Pure-function parser tests: no browser, no network."""

from __future__ import annotations

import pytest

from amazon_nl_mcp.amazon.extract import (
    asin_from_url,
    canonical_product_url,
    is_valid_asin,
    parse_int,
    parse_price,
    parse_rating,
)

BASE = "https://www.amazon.nl"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("€ 24,99", 24.99),
        ("€24,99", 24.99),
        ("EUR 1.299,00", 1299.00),
        ("€ 1.299,00", 1299.00),
        ("\u20ac\u00a099,95", 99.95),  # amazon.nl separates with a non-breaking space
        ("€ 5", 5.0),
        ("€ 0,99", 0.99),
    ],
)
def test_parse_price_dutch_formatting(text: str, expected: float) -> None:
    money = parse_price(text)
    assert money is not None
    assert money.amount == pytest.approx(expected)
    assert money.currency == "EUR"
    # _clean collapses the NBSP amazon.nl uses into an ordinary space.
    assert money.display == " ".join(text.split())


@pytest.mark.parametrize("text", ["", None, "Op voorraad", "1.284 beoordelingen", "4,4 van de 5"])
def test_parse_price_rejects_non_prices(text: str | None) -> None:
    assert parse_price(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4,4 van de 5 sterren", 4.4),
        ("4,5 van 5 sterren", 4.5),
        ("4.5 out of 5 stars", 4.5),
        ("5 van de 5 sterren", 5.0),
    ],
)
def test_parse_rating(text: str, expected: float) -> None:
    assert parse_rating(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", [None, "", "sterren", "9,9 van de 5 sterren"])
def test_parse_rating_rejects_nonsense(text: str | None) -> None:
    assert parse_rating(text) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [("1.284", 1284), ("1.284 beoordelingen", 1284), ("(2 345)", 2345), ("3", 3), ("geen", None)],
)
def test_parse_int(text: str, expected: int | None) -> None:
    assert parse_int(text) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.amazon.nl/dp/B0CX23V2ZK", "B0CX23V2ZK"),
        ("/Anker-kabel/dp/B0CX23V2ZK/ref=sr_1_1", "B0CX23V2ZK"),
        ("https://www.amazon.nl/gp/product/B0CX23V2ZK?psc=1", "B0CX23V2ZK"),
        ("https://www.amazon.nl/-/en/dp/b0cx23v2zk", "B0CX23V2ZK"),
        ("/sspa/click?url=%2Fdp%2FB09SPONSOR", None),
        ("https://www.amazon.nl/s?k=kabel", None),
    ],
)
def test_asin_from_url(url: str, expected: str | None) -> None:
    assert asin_from_url(url) == expected


def test_is_valid_asin() -> None:
    assert is_valid_asin("B0CX23V2ZK")
    assert not is_valid_asin("")
    assert not is_valid_asin("SHORT")
    assert not is_valid_asin(None)


def test_canonical_product_url_prefers_the_asin() -> None:
    messy = "/Anker-USB-C/dp/B0CX23V2ZK/ref=sr_1_1?keywords=usb&qid=123&sr=8-1"
    assert canonical_product_url(BASE, messy, "B0CX23V2ZK") == f"{BASE}/dp/B0CX23V2ZK"


def test_canonical_product_url_strips_tracking_when_asin_is_unknown() -> None:
    assert canonical_product_url(BASE, "/stores/Anker?ref=abc", None) == f"{BASE}/stores/Anker"
