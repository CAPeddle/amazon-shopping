"""Failure modes of the service, as exceptions the tool layer can translate.

The split matters at the MCP boundary: everything here is something the *caller*
should read and react to, so tools re-raise these as ``ToolError``. Anything not
in this module is a bug and stays in the log.
"""

from __future__ import annotations


class AmazonMCPError(Exception):
    """Base class for expected, caller-visible failures."""

    #: Short machine-readable tag, surfaced in structured errors and logs.
    code = "error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def as_text(self) -> str:
        return f"{self.message} {self.hint}".strip() if self.hint else self.message


class NotLoggedInError(AmazonMCPError):
    """The persistent profile has no valid amazon.nl session."""

    code = "not_logged_in"

    def __init__(self, message: str = "Not signed in to amazon.nl.") -> None:
        super().__init__(
            message,
            hint="Run `amazon-nl-mcp login` on the host to sign in interactively, then retry.",
        )


class BotWallError(AmazonMCPError):
    """Amazon served a CAPTCHA / 'Sorry, something went wrong' interstitial."""

    code = "bot_wall"

    def __init__(self, message: str = "amazon.nl served an automation check instead of the page.") -> None:
        super().__init__(
            message,
            hint=(
                "The service is backing off. Wait a minute and retry; if it persists, run "
                "`amazon-nl-mcp login` and clear the check by hand in the visible browser."
            ),
        )


class RateLimitedError(AmazonMCPError):
    """Local guardrail: too many browser-backed calls in the current window."""

    code = "rate_limited"

    def __init__(self, retry_after_s: float) -> None:
        self.retry_after_s = retry_after_s
        super().__init__(
            f"Local rate limit reached; retry in {retry_after_s:.0f}s.",
            hint="Raise AMAZON_MCP_RATE_LIMIT_PER_MINUTE if this is too strict for your use.",
        )


class ProductNotFoundError(AmazonMCPError):
    """A specific ASIN could not be loaded."""

    code = "product_not_found"

    def __init__(self, asin: str) -> None:
        self.asin = asin
        super().__init__(
            f"No product page found for ASIN {asin!r} on amazon.nl.",
            hint="Check the ASIN with amazon_search_products first; it may be a .com-only listing.",
        )


class ProductUnavailableError(AmazonMCPError):
    """The product exists but cannot be added to the cart."""

    code = "product_unavailable"


class VariantSelectionRequiredError(AmazonMCPError):
    """The listing is a parent ASIN; a size/colour child must be chosen first."""

    code = "variant_required"

    def __init__(self, asin: str, options: list[str] | None = None) -> None:
        self.asin = asin
        self.options = options or []
        shown = ", ".join(self.options[:8]) if self.options else "no options could be read"
        super().__init__(
            f"ASIN {asin} is a parent listing that needs a variant chosen first ({shown}).",
            hint="Call amazon_get_product on the ASIN and add one of the child ASINs it lists.",
        )


class CartVerificationError(AmazonMCPError):
    """The add-to-cart request returned, but the cart does not show the item."""

    code = "cart_not_updated"


class BrowserUnavailableError(AmazonMCPError):
    """Chromium could not be started or crashed and could not be recovered."""

    code = "browser_unavailable"


class WritesDisabledError(AmazonMCPError):
    """A mutation was attempted while the deployment is read-only."""

    code = "writes_disabled"

    def __init__(self) -> None:
        super().__init__(
            "Cart mutations are disabled on this deployment.",
            hint="Set AMAZON_MCP_WRITE_ENABLED=true and restart the service to allow them.",
        )


class PageTimeoutError(AmazonMCPError):
    """A navigation or interaction exceeded its deadline."""

    code = "timeout"
