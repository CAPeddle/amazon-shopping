"""Selector table for amazon.nl.

Amazon A/B tests its markup constantly, so every field is a *list* of candidate
selectors tried in order, most specific first. A field that no candidate matches
degrades to ``None`` rather than failing the call — a search result with no
readable price is still a useful search result.

When Amazon changes a layout, this file is the only place that should need
editing. Capture the new page into ``tests/fixtures/`` and the parser tests will
tell you which entry went stale.
"""

from __future__ import annotations

from typing import Final

# -- consent / interstitials ------------------------------------------------

COOKIE_ACCEPT: Final[list[str]] = [
    "#sp-cc-accept",
    "input[name='accept']",
    "[data-cel-widget='sp-cc-accept']",
    "button:has-text('Cookies accepteren')",
]

CAPTCHA_MARKERS: Final[list[str]] = [
    "form[action*='/errors/validateCaptcha']",
    "input#captchacharacters",
    "img[src*='captcha']",
    "#cvf-page-content",  # 'Enter the characters you see' / OTP challenge
]

#: Phrases that mean "prove you are human", and nothing else.
#:
#: Deliberately narrow. Amazon's 404 page and its bot wall share the wording
#: "Sorry, er is iets misgegaan", so matching on that would let a mistyped ASIN
#: trip the circuit breaker and take the whole service offline for the cooldown.
#: Anything ambiguous is left to the structural markers above.
CAPTCHA_TEXT_MARKERS: Final[tuple[str, ...]] = (
    "voer de tekens in die je hieronder ziet",
    "voer de tekens in die u hieronder ziet",
    "voer de karakters in die u hieronder ziet",
    "type de tekens die je hieronder ziet",
    "enter the characters you see below",
    "geef ons de kans het goed te maken",
    "om verder te gaan, bevestig dat je geen robot bent",
    "to continue, please confirm that you are not a robot",
)

# 'Continue shopping' wall that Amazon serves instead of a page under load.
CONTINUE_SHOPPING: Final[list[str]] = [
    "button:has-text('Verder winkelen')",
    "button:has-text('Continue shopping')",
    "input[type='submit'][value='Verder winkelen']",
    "alt[type='submit']",
]

DELIVERY_LOCATION_DISMISS: Final[list[str]] = [
    "input[data-action-type='DISMISS']",
    "button[data-action-type='DISMISS']",
    ".a-popover-footer .a-button-close",
]

# -- account / nav ----------------------------------------------------------

NAV_ACCOUNT_GREETING: Final[list[str]] = [
    "#nav-link-accountList-nav-line-1",
    "#nav-link-accountList .nav-line-1",
    "#nav-link-accountList",
]

NAV_CART_COUNT: Final[list[str]] = [
    "#nav-cart-count",
    "#nav-cart-count-container #nav-cart-count",
    "[data-cart-count]",
]

#: Second, independent read of the cart size: '3 items in winkelwagen'.
NAV_CART_ARIA: Final[list[str]] = ["#nav-cart"]

#: Presence of this attribute on the account nav link means signed OUT — it is
#: dropped once the session is authenticated, which makes it the single most
#: reliable tell on the page.
NAV_SIGNIN_ROLE: Final[str] = "#nav-link-accountList[data-nav-role='signin']"

NAV_ACCOUNT_LINK: Final[str] = "#nav-link-accountList"

#: Exact greeting strings. Never substring-match "Hallo" — it prefixes both states.
SIGNED_OUT_MARKERS: Final[tuple[str, ...]] = (
    "hallo, inloggen",
    "hello, sign in",
    "inloggen",
    "sign in",
    "account en lijsten",
)

SIGN_IN_PAGE_MARKERS: Final[list[str]] = [
    "form[name='signIn']",
    "#ap_email",
    "#ap_password",
    "input[name='email'][type='email']",
]

# -- search results ---------------------------------------------------------

SEARCH_RESULT_ROW: Final[list[str]] = [
    "div[data-component-type='s-search-result'][data-asin]:not([data-asin=''])",
    "div.s-result-item[data-asin]:not([data-asin=''])",
    "[role='listitem'][data-asin]:not([data-asin=''])",
]

RESULT_TITLE: Final[list[str]] = [
    "[data-cy='title-recipe'] h2 span",
    "h2 a span",
    "h2 span",
    "h2",
    ".a-size-medium.a-color-base.a-text-normal",
]

RESULT_LINK: Final[list[str]] = [
    "[data-cy='title-recipe'] a.a-link-normal",
    "h2 a.a-link-normal",
    "a.a-link-normal.s-no-outline",
    "a.a-link-normal[href*='/dp/']",
]

RESULT_PRICE_DISPLAY: Final[list[str]] = [
    "[data-cy='price-recipe'] .a-price[data-a-color='base'] .a-offscreen",
    "[data-cy='price-recipe'] .a-price:not(.a-text-price) .a-offscreen",
    ".a-price:not(.a-text-price):not([data-a-strike='true']) .a-offscreen",
    ".a-price .a-offscreen",
]

RESULT_PRICE_WHOLE: Final[list[str]] = [".a-price .a-price-whole"]
RESULT_PRICE_FRACTION: Final[list[str]] = [".a-price .a-price-fraction"]

RESULT_RATING: Final[list[str]] = [
    "[data-cy='reviews-block'] i[class*='a-star-'] .a-icon-alt",
    "i[class*='a-star-'] .a-icon-alt",
    "[data-cy='reviews-block'] .a-icon-alt",
    ".a-icon-star .a-icon-alt",
    "[aria-label*='sterren']",
    "[aria-label*='out of 5 stars']",
]

RESULT_REVIEW_COUNT: Final[list[str]] = [
    "[data-cy='reviews-block'] a .a-size-base",
    "a[href*='#customerReviews'] span.a-size-base",
    "span[aria-label$='beoordelingen']",
    ".s-underline-text",
]

RESULT_IMAGE: Final[list[str]] = ["img.s-image", "img[data-image-latency]", ".s-image"]

RESULT_PRIME: Final[list[str]] = [
    "i.a-icon-prime",
    "[aria-label='Prime']",
    ".s-prime",
    "[data-cy='delivery-recipe'] i.a-icon-prime",
]

RESULT_SPONSORED: Final[list[str]] = [
    ".puis-sponsored-label-text",
    "[data-component-type='sp-sponsored-result']",
    "a[href^='/sspa/click']",
    "a[aria-label*='Gesponsord']",
]

#: A class on the result container itself, checked separately from the
#: descendant selectors above.
SPONSORED_CONTAINER_CLASS: Final[str] = "AdHolder"

RESULT_AVAILABILITY: Final[list[str]] = [
    "[data-cy='delivery-recipe'] .a-color-price",
    ".a-color-price:has-text('voorraad')",
    ".a-color-success",
]

SEARCH_NEXT_PAGE: Final[list[str]] = [
    "a.s-pagination-next:not(.s-pagination-disabled)",
    "a[aria-label='Ga naar volgende pagina']",
    "a[aria-label='Go to next page']",
]

NO_RESULTS_MARKERS: Final[tuple[str, ...]] = (
    "geen resultaten voor",
    "no results for",
    "probeer het opnieuw met een andere zoekopdracht",
)

# -- product detail page ----------------------------------------------------

PDP_TITLE: Final[list[str]] = ["#productTitle", "#title span", "h1#title"]

PDP_PRICE_DISPLAY: Final[list[str]] = [
    "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price):not([data-a-strike='true']) .a-offscreen",
    "#apex_desktop .a-price:not(.a-text-price):not([data-a-strike='true']) .a-offscreen",
    "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#corePrice_desktop .a-price .a-offscreen",
    "#price_inside_buybox",
    "#newBuyBoxPrice",
    ".a-price .a-offscreen",
]

#: Hidden input carrying the ASIN the page actually resolved to. Catches the case
#: where /dp/<child> silently redirected to a variation parent.
PDP_ASIN_INPUT: Final[list[str]] = ["input#ASIN", "input[name='ASIN']", "#dp [data-asin]"]

PDP_OUT_OF_STOCK: Final[list[str]] = ["#outOfStock", "#buybox-see-all-buying-choices"]

#: Availability wording amazon.nl uses for a listing that cannot be bought now.
UNAVAILABLE_TEXT_MARKERS: Final[tuple[str, ...]] = (
    "momenteel niet verkrijgbaar",
    "tijdelijk niet op voorraad",
    "niet op voorraad",
    "currently unavailable",
    "we weten niet of en wanneer dit item weer op voorraad is",
)

PDP_AVAILABILITY: Final[list[str]] = [
    "#availability span.a-color-success",
    "#availability span",
    "#availability",
    "#outOfStock .a-color-price",
]

PDP_ADD_TO_CART: Final[list[str]] = [
    "#add-to-cart-button",
    "input#add-to-cart-button",
    "#addToCart input[name='submit.add-to-cart']",
    "input[name='submit.add-to-cart']",
]

PDP_QUANTITY_SELECT: Final[list[str]] = [
    "select#quantity",
    "#quantity",
    "select[name='quantity']",
]

PDP_IMAGE: Final[list[str]] = [
    "#landingImage",
    "#imgTagWrapperId img",
    "#main-image-container img",
    "#ebooksImgBlkFront",
]

PDP_BYLINE: Final[list[str]] = ["#bylineInfo", "#brand", "a#bylineInfo"]

PDP_RATING: Final[list[str]] = [
    "#acrPopover .a-icon-alt",
    "#averageCustomerReviews .a-icon-alt",
    "span[data-hook='rating-out-of-text']",
    "#acrPopover",
]

PDP_REVIEW_COUNT: Final[list[str]] = [
    "#acrCustomerReviewText",
    "[data-hook='total-review-count']",
]

PDP_BULLETS: Final[list[str]] = [
    "#feature-bullets li span.a-list-item",
    "#feature-bullets li",
    "#productFactsDesktopExpander li span",
]

PDP_VARIANT_ITEMS: Final[list[str]] = [
    "#twister_feature_div li[data-asin]:not([data-asin=''])",
    "#twister li[data-defaultasin]",
    "#twister li[data-dp-url]",
    "#variation_size_name li",
    "#variation_color_name li",
    "#twisterContainer li[data-asin]",
    "[id^='inline-twister'] li[data-asin]",
]

PDP_DOG_PAGE_MARKERS: Final[tuple[str, ...]] = (
    "sorry! er is iets misgegaan",
    "er is iets misgegaan",
    "de pagina die je zoekt",
    "deze pagina is niet gevonden",
    "looking for something?",
    "we couldn't find that page",
    "page not found",
)

# -- post-add interstitials -------------------------------------------------

ADD_CONFIRMATION_MARKERS: Final[list[str]] = [
    "#huc-v2-order-row-confirm-text",
    "#attach-added-to-cart-message",
    "#sw-atc-details-single-container",
    "[data-feature-id='huc-atc-status'] .a-alert-success",
    "#NATC_SMART_WAGON_CONF_MSG_SUCCESS",
]

WARRANTY_DECLINE: Final[list[str]] = [
    "#attachSiNoCoverage input[type='submit']",
    "input[aria-labelledby='attachSiNoCoverage-announce']",
    "#attachSiNoCoverage",
    "#attach-sidesheet-view-cart-button",
    "input[name='attachSiNoCoverage']",
    "button:has-text('Nee, bedankt')",
]

VARIANT_REQUIRED_MARKERS: Final[tuple[str, ...]] = (
    "kies een optie",
    "selecteer een optie",
    "select an option",
    "kies een maat",
)

# -- cart -------------------------------------------------------------------

CART_LINE: Final[list[str]] = [
    "div[data-name='Active Items'] div[data-asin]:not([data-asin=''])",
    "div.sc-list-item[data-asin]:not([data-asin=''])",
    "[data-item-index][data-asin]:not([data-asin=''])",
    "div[data-itemtype='active'][data-asin]",
]

CART_LINE_TITLE: Final[list[str]] = [
    ".sc-product-title",
    "[data-a-selector='title'] .a-truncate-full",
    ".sc-grid-item-product-title span",
    "span.a-truncate-full",
    "a.sc-product-link span",
]

CART_LINE_PRICE: Final[list[str]] = [
    ".sc-badge-price-to-pay .a-offscreen",
    ".sc-product-price",
    ".sc-price",
    ".a-price .a-offscreen",
]

CART_LINE_QUANTITY_SELECT: Final[list[str]] = [
    "select[name='quantity']",
    ".sc-quantity-select select",
]

CART_LINE_QUANTITY_INPUT: Final[list[str]] = [
    "input[name='quantityBox']",
    "input.sc-quantity-textfield",
    "input[data-a-selector='value']",
]

CART_LINE_QUANTITY_TEXT: Final[list[str]] = [
    "[data-a-selector='value'] .a-dropdown-prompt",
    ".a-dropdown-prompt",
    "span.sc-quantity-textfield",
    "span[data-a-selector='value']",
]

CART_LINE_LINK: Final[list[str]] = ["a.sc-product-link", "a[href*='/dp/']"]

CART_LINE_AVAILABILITY: Final[list[str]] = [".sc-product-availability", ".a-color-price"]

CART_SUBTOTAL: Final[list[str]] = [
    "#sc-subtotal-amount-activecart .a-price .a-offscreen",
    "#sc-subtotal-amount-activecart",
    "#sc-subtotal-amount-buybox",
    "[data-name='Subtotals'] .a-price .a-offscreen",
]

CART_ITEM_COUNT_TEXT: Final[list[str]] = [
    "#sc-subtotal-label-activecart",
    "#sc-subtotal-label-buybox",
]

CART_EMPTY_MARKERS: Final[tuple[str, ...]] = (
    "je amazon-winkelwagen is leeg",
    "je winkelwagen is leeg",
    "your amazon cart is empty",
    "uw winkelwagen is leeg",
)
