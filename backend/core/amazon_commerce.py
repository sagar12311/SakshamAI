"""Narrow, deterministic Amazon.in commerce flow.

This module deliberately is not a general web agent.  It owns a single
conversation-bound Chrome tab, uses only fixed scripts, accepts only Amazon.in
pages and can prepare exactly one COD order snapshot.  The final dispatch is
admin-only and starts with a rehearsal; it never retries a Place Order click.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from config import get_settings
from core.state_store import get_state_store


_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_PRICE_RE = re.compile(r"(?:₹|rs\.?|inr\s*)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", re.I)
_SESSION_KEY = "amazon_commerce_live_order_gate_v1"
_MAX_PRODUCTS = 12
_MAX_OBSERVED_PRODUCTS = 24
_TITLE_MAX = 180


# These scripts are code-owned. Product page text is returned solely as bounded
# JSON data; it is never passed into the model's action-planning prompt.
_READ_SEARCH_RESULTS = r"""(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 240);
  const rows = Array.from(document.querySelectorAll('[data-component-type="s-search-result"][data-asin]'));
  const products = rows.slice(0, 24).map(row => {
    const rect = row.getBoundingClientRect();
    return {
      asin: clean(row.getAttribute('data-asin')).toUpperCase(),
      title: clean((row.querySelector('h2') || row.querySelector('[data-cy="title-recipe"]'))?.textContent),
      price: clean(row.querySelector('.a-price .a-offscreen')?.textContent),
      rating: clean(row.querySelector('.a-icon-alt')?.textContent),
      seller: clean(row.querySelector('.a-row.a-size-base.a-color-secondary')?.textContent),
      visible: rect.bottom > 0 && rect.top < window.innerHeight
    };
  }).filter(item => /^[A-Z0-9]{10}$/.test(item.asin) && item.title && item.price);
  return JSON.stringify({page: location.pathname, products});
})()"""

_SCROLL_AND_READ_SEARCH_RESULTS = (
    "window.scrollBy(0, Math.max(650, Math.floor(window.innerHeight * 0.85)));"
    + _READ_SEARCH_RESULTS
)

_OPEN_ASIN = r"""(asin => {
  if (!/^[A-Z0-9]{10}$/.test(asin)) return 'invalid_asin';
  location.assign('https://www.amazon.in/dp/' + asin);
  return 'navigating';
})(%s)"""

# Opening the cart is navigation only.
_OPEN_CART = "location.assign('https://www.amazon.in/gp/cart/view.html'); 'navigating'"

# Number only cards already present in the tracked Amazon search DOM.  The
# labels exist locally in Chrome for the current viewport and are not sent to
# Amazon or persisted.  ASINs originate from the bounded DOM reader and are
# revalidated before interpolation.
_LABEL_VISIBLE_PRODUCTS = r"""(labels => {
  document.querySelectorAll('[data-saksham-product-label="true"]').forEach(node => node.remove());
  for (const row of Array.from(document.querySelectorAll('[data-asin]'))) {
    const asin = String(row.getAttribute('data-asin') || '').toUpperCase();
    const number = labels[asin];
    const rect = row.getBoundingClientRect();
    if (!Number.isInteger(number) || rect.bottom <= 0 || rect.top >= window.innerHeight) continue;
    const badge = document.createElement('div');
    badge.dataset.sakshamProductLabel = 'true';
    badge.textContent = 'Item ' + number;
    badge.setAttribute('aria-label', 'Item number ' + number);
    Object.assign(badge.style, {
      position: 'absolute', top: '8px', left: '8px', zIndex: '2147483647',
      background: '#111827', color: '#ffffff', border: '2px solid #ffffff',
      borderRadius: '999px', padding: '5px 9px', font: '700 14px Arial',
      pointerEvents: 'none', boxShadow: '0 1px 5px rgba(0,0,0,.45)'
    });
    const originalPosition = getComputedStyle(row).position;
    if (originalPosition === 'static') row.style.position = 'relative';
    row.appendChild(badge);
  }
  return JSON.stringify({ok:true});
})(%s)"""

_ADD_TO_CART = r"""(() => {
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const candidates = Array.from(document.querySelectorAll('input, button, a'));
  const control = candidates.find(node => {
    const label = norm(node.getAttribute('aria-label') || node.getAttribute('value') || node.textContent);
    return label === 'add to cart' || label === 'add to shopping cart';
  });
  if (!control) return JSON.stringify({ok:false, reason:'add_to_cart_control_not_found'});
  control.click();
  return JSON.stringify({ok:true, action:'add_to_cart_clicked_once'});
})()"""

_BUY_NOW_PREVIEW = r"""(() => {
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const candidates = Array.from(document.querySelectorAll('input, button, a'));
  const button = candidates.find(node => {
    const label = norm(node.getAttribute('aria-label') || node.getAttribute('value') || node.textContent);
    return label === 'buy now' || label === 'buy now with 1-click';
  });
  if (!button) return JSON.stringify({ok:false, reason:'buy_now_control_not_found'});
  button.click();
  return JSON.stringify({ok:true, action:'checkout_preview_opened'});
})()"""

_SELECT_COD = r"""(() => {
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const labels = Array.from(document.querySelectorAll('label, span, div'));
  const label = labels.find(node => norm(node.textContent).includes('cash on delivery'));
  if (!label) return JSON.stringify({ok:false, reason:'cod_not_available'});
  const forId = label.getAttribute('for');
  const input = (forId && document.getElementById(forId)) || label.querySelector('input[type="radio"]');
  if (!input) return JSON.stringify({ok:false, reason:'cod_control_not_found'});
  input.click();
  input.dispatchEvent(new Event('change', {bubbles:true}));
  return JSON.stringify({ok:true, action:'cod_selected'});
})()"""

_READ_CHECKOUT = r"""(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  const textFor = selectors => {
    for (const selector of selectors) {
      const node = document.querySelector(selector);
      const value = clean(node && node.textContent);
      if (value) return value;
    }
    return '';
  };
  const body = clean(document.body && document.body.innerText).slice(0, 18000);
  const byLabel = label => {
    const index = body.toLowerCase().indexOf(label.toLowerCase());
    return index >= 0 ? body.slice(index, index + 140) : '';
  };
  const place = Array.from(document.querySelectorAll('input,button,a')).find(node => {
    const value = clean(node.getAttribute('aria-label') || node.getAttribute('value') || node.textContent).toLowerCase();
    return value === 'place your order' || value === 'place order';
  });
  const address = textFor(['#address-book-entry-0', '.ship-to-this-address', '[data-testid="AddressText"]']);
  return JSON.stringify({
    page: location.pathname,
    title: textFor(['#productTitle', '.a-truncate-cut', '[data-testid="item-title"]']),
    seller: textFor(['#merchantInfo', '.offer-display-feature-text-message', '[data-testid="seller-name"]']),
    item_price: textFor(['#subtotals-marketplace-table .a-row:nth-child(1) .a-text-right', '[data-testid="subtotal"]']) || byLabel('Items:'),
    delivery_fee: textFor(['#subtotals-marketplace-table .a-row:nth-child(2) .a-text-right', '[data-testid="shipping"]']) || byLabel('Delivery:'),
    total: textFor(['#subtotals-marketplace-table .a-color-price', '#grand-total-price', '[data-testid="order-total"]']) || byLabel('Order Total:'),
    address: address,
    place_order_available: Boolean(place)
  });
})()"""

_PLACE_ORDER = r"""(() => {
  const norm = value => String(value || '').replace(/\s+/g, ' ').trim().toLowerCase();
  const candidates = Array.from(document.querySelectorAll('input,button,a'));
  const place = candidates.find(node => {
    const label = norm(node.getAttribute('aria-label') || node.getAttribute('value') || node.textContent);
    return label === 'place your order' || label === 'place order';
  });
  if (!place) return JSON.stringify({ok:false, reason:'place_order_control_not_found'});
  place.click();
  return JSON.stringify({ok:true, action:'place_order_clicked_once'});
})()"""

_READ_ORDER_CONFIRMATION = r"""(() => {
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  const body = clean(document.body && document.body.innerText).slice(0, 12000);
  const reference = (body.match(/order\s*(?:#|number|no\.?)[\s:]*([0-9-]{8,})/i) || [])[1] || '';
  const confirmed = /order\s+(?:has\s+been\s+)?placed|thank you.*order|order\s+confirmed/i.test(body);
  return JSON.stringify({confirmed, order_reference: reference});
})()"""


class AmazonCommerceError(ValueError):
    """A controlled commerce refusal; callers must not guess a recovery."""


@dataclass
class CommerceSession:
    conversation_id: str
    tab_id: str
    max_price_inr: int
    query: str
    created_at: float = field(default_factory=time.monotonic)
    stage: str = "search"
    # Bounded, untrusted descriptions from the current tracked results page.
    # `products` is the price-qualified subset that may be selected; observed
    # products exist only to explain an out-of-budget request clearly.
    observed_products: list[dict[str, Any]] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)
    selected: Optional[dict[str, Any]] = None
    order_snapshot: Optional[dict[str, Any]] = None


class AmazonCommerceService:
    """Keep a bounded, conversation-local Amazon COD flow out of the LLM."""

    def __init__(
        self, *, browser: Any = None, settings: Any = None, store: Any = None,
        screen_observer: Any = None,
    ) -> None:
        self._browser = browser
        self._settings = settings or get_settings()
        self._store = store
        self._screen_observer = screen_observer
        self._sessions: dict[str, CommerceSession] = {}

    def set_browser(self, browser: Any) -> None:
        self._browser = browser

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "amazon_commerce_enabled", False))

    @property
    def session_ttl_seconds(self) -> int:
        return max(60, int(getattr(self._settings, "amazon_commerce_session_ttl_seconds", 900)))

    @property
    def hard_total_cap(self) -> int:
        # This cap is product safety policy, not an LLM parameter.
        return min(500, max(1, int(getattr(self._settings, "amazon_commerce_max_total_inr", 500))))

    @property
    def requires_screen_observation(self) -> bool:
        return bool(getattr(self._settings, "amazon_commerce_require_screen_observation", False))

    async def plan_turn(self, user_input: str, conversation_id: str) -> Optional[dict[str, Any]]:
        """Return a deterministic plan/answer for a supported commerce turn."""
        if not self.enabled:
            return None
        # This is intentionally a tiny, shopping-only lexicon. Speech-to-text
        # often renders “bedsheet” as an offensive-sounding homophone; applying
        # this correction here means it can influence only the Amazon flow, not
        # chat history, terminal commands, contacts, or other personal tasks.
        text = self._normalise_spoken_commerce_text(str(user_input or "").strip())
        if not text:
            return None
        search = self._parse_search_request(text)
        if search:
            query, max_price = search
            return {
                "understood_intent": "Search Amazon.in products",
                "plan": [{
                    "action": "amazon_search", "target": query,
                    "parameters": {"max_price_inr": max_price},
                    "description": "Search Amazon.in and show products within the requested price range",
                }],
                "response": "I’ll search Amazon.in and keep the visible options within your price limit.",
                "commerce": True,
            }

        lowered = text.lower().strip()
        wants_cart = self._is_cart_request(lowered)
        wants_add_to_cart = self._is_add_to_cart_request(lowered)
        open_request = self._parse_open_request(text)
        session = self._session(conversation_id)
        if not session and (wants_cart or open_request is not None or self._parse_scroll_direction(lowered) is not None):
            # A code reload must not turn a product title into an application
            # name. Recover only the *currently visible*, HTTPS Amazon.in
            # search tab, then reconstruct the bounded product snapshot.
            session = await self._adopt_visible_amazon_search(conversation_id)
        if not session:
            return None
        # Voice transcripts commonly vary the word order: "scroll down",
        # "scroll down further", and "scroll further down" must all keep
        # operating on the same tracked tab.  Do not fall through to the LLM
        # for a near-identical scroll phrase.
        direction = self._parse_scroll_direction(lowered)
        if direction is not None:
            return self._plan("Scroll the tracked Amazon search results", "browser_scroll", "", {"direction": direction})

        if wants_cart:
            return self._plan("Open the tracked Amazon cart without changing it", "amazon_open_cart", "", {})

        if wants_add_to_cart:
            if session.stage == "product_open" and session.selected:
                return self._plan(
                    "Add the selected Amazon item to the cart",
                    "amazon_add_to_cart",
                    session.selected["asin"],
                    {"asin": session.selected["asin"]},
                )
            return self._simple("Open one visible product first, then say ‘add it to cart’. Cart changes require admin activation.")

        if open_request is not None:
            product, error = self._resolve_product(session, open_request)
            if error:
                return self._simple(error)
            return self._plan(
                "Open the selected Amazon product",
                "amazon_open_product",
                product["asin"],
                {"asin": product["asin"]},
            )

        if self._is_buy_request(lowered):
            if session.stage == "product_open" and session.selected:
                return self._plan(
                    "Open a checkout preview for one item",
                    "amazon_checkout_preview",
                    session.selected["asin"],
                    {"asin": session.selected["asin"], "quantity": 1},
                )
            if session.stage == "order_ready" and session.order_snapshot:
                return self._place_order_plan(session)
            return self._simple("Open one product first, then say ‘buy it’. I will only prepare a one-item checkout preview.")

        if "cash on delivery" in lowered or lowered in {"cod", "cash"}:
            if session.stage != "checkout_open":
                return self._simple("I need the checkout preview before choosing payment. Say ‘buy it’ after opening a product.")
            return self._plan(
                "Select Cash on Delivery for the preview",
                "amazon_select_payment",
                "cod",
                {"method": "cod"},
            )

        if any(phrase in lowered for phrase in ("place order", "place the order", "proceed with order", "go ahead with order")):
            if session.stage == "order_ready" and session.order_snapshot:
                return self._place_order_plan(session)
            return self._simple("I need a verified COD checkout summary before I can prepare the admin activation.")

        if any(payment in lowered for payment in ("upi", "card", "credit", "debit", "gift", "balance", "address", "quantity")):
            return self._simple("For this COD test I cannot change payment methods, addresses, or quantity. It is limited to one Cash on Delivery item.")
        return None

    async def search(self, query: str, max_price_inr: Any, conversation_id: str) -> dict[str, Any]:
        self._require_browser()
        query = self._normalise_query(query)
        limit = self._normalise_limit(max_price_inr)
        url = "https://www.amazon.in/s?k=" + urllib.parse.quote_plus(query, safe="")
        opened = await self._browser.open_tracked_chrome_tab(url)
        if not opened.get("success"):
            return self._failure("Amazon search could not open a tracked Chrome tab")
        tab_id = str(opened.get("tab_id") or "")
        if not tab_id.isdigit():
            return self._failure("Amazon search did not receive a valid Chrome tab")
        session = CommerceSession(conversation_id=conversation_id, tab_id=tab_id, max_price_inr=limit, query=query)
        self._sessions[conversation_id] = session
        await self._refresh_products(session, wait_for_page=True)
        screen_labels_shown = await self._show_visible_product_numbers(session)
        products = self._visible_products(session)
        message = self._products_message(session)
        return {
            "success": True,
            "action": "amazon_search",
            "trust": "untrusted_web",
            "query": query,
            "max_price_inr": limit,
            "products": products,
            "result_count": len(products),
            "screen_labels_shown": screen_labels_shown,
            "message": message,
        }

    async def scroll(self, direction: str, conversation_id: str) -> dict[str, Any]:
        session = self._require_session(conversation_id)
        await self._require_page(session, "search")
        direction = str(direction or "down").lower()
        if direction not in {"down", "up", "top", "bottom"}:
            return self._failure("Unsupported scroll direction", action="browser_scroll")
        # A combined fixed script keeps the update tied to the same pinned tab.
        response = await self._browser.execute_javascript_in_chrome_tab(
            session.tab_id,
            _SCROLL_AND_READ_SEARCH_RESULTS if direction == "down" else self._scroll_script(direction),
        )
        if not response.get("success"):
            return self._failure("Could not scroll the tracked Amazon tab", action="browser_scroll")
        self._update_product_snapshot(session, response.get("result"))
        screen_labels_shown = await self._show_visible_product_numbers(session)
        return {
            "success": True, "action": "browser_scroll", "trust": "untrusted_web",
            "direction": direction, "products": self._visible_products(session),
            "result_count": len(self._visible_products(session)),
            "screen_labels_shown": screen_labels_shown,
            "message": self._products_message(session),
        }

    async def open_product(self, asin: str, conversation_id: str) -> dict[str, Any]:
        session = self._require_session(conversation_id)
        await self._require_page(session, "search")
        asin = self._validate_asin(asin)
        # Re-read both the tracked DOM viewport and the real local display
        # immediately before opening.  A stale chat response must not select
        # something that has since scrolled away or been covered by another UI.
        await self._refresh_products(session, wait_for_page=False)
        selected = next((item for item in session.products if item["asin"] == asin), None)
        if not selected:
            return self._failure("That product is not in the current tracked results", action="amazon_open_product")
        if not selected.get("visible"):
            return self._failure("That product is not currently visible in the tracked Amazon viewport. Ask me to scroll to it first.", action="amazon_open_product")
        screen_verification = await self._verify_screen_visible_product(session, selected)
        if not screen_verification.get("success"):
            return self._failure(str(screen_verification.get("error") or "Screen observation could not verify the selected product"), action="amazon_open_product")
        script = _OPEN_ASIN % json.dumps(asin)
        opened = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, script)
        if not opened.get("success") or str(opened.get("result")) == "invalid_asin":
            return self._failure("Could not open the selected Amazon product", action="amazon_open_product")
        session.selected = dict(selected)
        session.stage = "product_open"
        return {
            "success": True, "action": "amazon_open_product", "trust": "untrusted_web",
            "product": dict(selected), "screen_verified": bool(screen_verification.get("screen_verified")),
            "message": "Selected visible product opened in the tracked Amazon tab",
        }

    async def open_cart(self, conversation_id: str) -> dict[str, Any]:
        """Open the pinned user's Amazon cart without reading or modifying it."""
        session = self._require_session(conversation_id)
        await self._require_secure_amazon_tab(session)
        opened = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, _OPEN_CART)
        if not opened.get("success"):
            return self._failure("Could not open the tracked Amazon cart", action="amazon_open_cart")
        return {
            "success": True,
            "action": "amazon_open_cart",
            "message": "Opened the tracked Amazon cart. No items were added or changed.",
        }

    async def add_to_cart(self, asin: str, conversation_id: str) -> dict[str, Any]:
        """Click the exact Add to Cart control once after live admin approval."""
        session = self._require_session(conversation_id)
        asin = self._validate_asin(asin)
        if not session.selected or session.selected.get("asin") != asin:
            return self._failure("The selected product no longer matches the cart request")
        await self._require_page(session, "product", asin=asin)
        screen_verification = await self._verify_screen_visible_product(session, session.selected)
        if not screen_verification.get("success"):
            return self._failure(str(screen_verification.get("error") or "Screen observation could not verify the selected product"), action="amazon_add_to_cart")
        response = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, _ADD_TO_CART)
        payload = self._decode_json(response.get("result"))
        if not response.get("success") or not payload.get("ok"):
            return self._failure("Amazon did not show the expected Add to Cart control; nothing was changed", action="amazon_add_to_cart")
        return {
            "success": True,
            "action": "amazon_add_to_cart",
            "product": {"asin": asin, "title": session.selected.get("title", "selected item")},
            "screen_verified": bool(screen_verification.get("screen_verified")),
            "message": "Amazon accepted one Add to Cart click for the selected visible item.",
        }

    async def checkout_preview(self, asin: str, quantity: Any, conversation_id: str) -> dict[str, Any]:
        session = self._require_session(conversation_id)
        if int(quantity) != 1:
            return self._failure("This test permits exactly one item", action="amazon_checkout_preview")
        asin = self._validate_asin(asin)
        if not session.selected or session.selected.get("asin") != asin:
            return self._failure("The selected product no longer matches the checkout request", action="amazon_checkout_preview")
        await self._require_page(session, "product", asin=asin)
        response = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, _BUY_NOW_PREVIEW)
        payload = self._decode_json(response.get("result"))
        if not response.get("success") or not payload.get("ok"):
            return self._failure("Checkout preview stopped because Amazon did not show the expected Buy Now control", action="amazon_checkout_preview")
        session.stage = "checkout_open"
        return {
            "success": True, "action": "amazon_checkout_preview", "asin": asin, "quantity": 1,
            "trust": "untrusted_web",
            "message": "Checkout preview is open. Which payment method should I use? For this test, only Cash on Delivery is supported.",
        }

    async def select_payment(self, method: str, conversation_id: str) -> dict[str, Any]:
        session = self._require_session(conversation_id)
        if str(method or "").lower() != "cod":
            return self._failure("Only Cash on Delivery is supported for this test")
        if session.stage != "checkout_open" or not session.selected:
            return self._failure("A checkout preview is required before selecting Cash on Delivery")
        await self._require_page(session, "checkout")
        chosen = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, _SELECT_COD)
        choice = self._decode_json(chosen.get("result"))
        if not chosen.get("success") or not choice.get("ok"):
            return self._failure("Cash on Delivery is unavailable or Amazon's payment layout changed")
        snapshot = await self._read_order_snapshot(session, require_place_control=True)
        session.order_snapshot = snapshot
        session.stage = "order_ready"
        return {
            "success": True, "action": "amazon_select_payment", "method": "cod",
            "trust": "untrusted_web",
            "order_snapshot": dict(snapshot), "message": self._snapshot_message(snapshot),
        }

    async def place_order(self, expected_snapshot: Any, conversation_id: str) -> dict[str, Any]:
        session = self._require_session(conversation_id)
        if session.stage != "order_ready" or not session.order_snapshot:
            return self._failure("There is no pending COD order snapshot")
        expected = self._normalise_snapshot(expected_snapshot)
        if expected != session.order_snapshot:
            return self._failure("The order snapshot changed before authorization")
        current = await self._read_order_snapshot(session, require_place_control=True)
        if current != expected:
            return self._failure("Amazon checkout changed after the order was summarized; no order was placed")
        if current["total_inr"] > min(session.max_price_inr, self.hard_total_cap):
            return self._failure("The final total exceeds the requested or ₹500 safety limit")

        gate = await self._get_gate()
        if gate == "rehearsal_required":
            await self._set_gate("armed_one_live_order")
            # A rehearsal can never be converted into an order by repeating
            # the same prepared plan. Start a new explicit shopping flow.
            session.stage = "rehearsal_complete"
            session.order_snapshot = None
            return {
                "success": True, "action": "amazon_place_order", "event": "AMAZON_REHEARSAL_COMPLETED",
                "events": ["AMAZON_REHEARSAL_COMPLETED"], "mode": "rehearsal", "trust": "untrusted_web", "order_snapshot": current,
                "message": "Rehearsal complete. The Place Order control was verified and was not clicked. One future explicit COD test is armed.",
            }
        if gate != "armed_one_live_order":
            return self._failure("The one-order COD test is disarmed; no order was placed")

        clicked = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, _PLACE_ORDER)
        click_result = self._decode_json(clicked.get("result"))
        if not clicked.get("success") or not click_result.get("ok"):
            # A failed pre-click leaves the gate usable; no dispatch evidence exists.
            return self._failure("The final Place Order control was not available; no order was placed")

        # A click could have dispatched even when navigation times out. Disarm
        # before observing the result and never retry automatically.
        await self._set_gate("disarmed")
        await asyncio.sleep(1.0)
        confirmation = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, _READ_ORDER_CONFIRMATION)
        status = self._decode_json(confirmation.get("result")) if confirmation.get("success") else {}
        if status.get("confirmed"):
            return {
                "success": True, "action": "amazon_place_order", "event": "AMAZON_ORDER_CONFIRMED",
                "events": ["AMAZON_ORDER_DISPATCHED", "AMAZON_ORDER_CONFIRMED"],
                "mode": "live", "trust": "untrusted_web", "order_reference": self._mask_order_reference(status.get("order_reference")),
                "order_snapshot": current, "message": "COD order confirmation detected.",
            }
        return {
            "success": False, "action": "amazon_place_order", "event": "AMAZON_ORDER_STATUS_UNKNOWN",
            "events": ["AMAZON_ORDER_DISPATCHED", "AMAZON_ORDER_STATUS_UNKNOWN"],
            "mode": "live", "trust": "untrusted_web", "order_snapshot": current,
            "error": "Order status is unknown after a single dispatch attempt. Live ordering is now disabled; inspect Your Orders manually.",
        }

    def _plan(self, intent: str, action: str, target: str, parameters: dict[str, Any]) -> dict[str, Any]:
        return {"understood_intent": intent, "plan": [{"action": action, "target": target, "parameters": parameters, "description": intent}], "commerce": True}

    def _place_order_plan(self, session: CommerceSession) -> dict[str, Any]:
        return self._plan(
            "Place the prepared one-item Cash on Delivery order",
            "amazon_place_order",
            session.order_snapshot["asin"],
            {"order_snapshot": dict(session.order_snapshot)},
        )

    @staticmethod
    def _simple(text: str) -> dict[str, Any]:
        return {"is_simple_response": True, "response": text, "commerce": True, "untrusted_web_derived": True}

    @staticmethod
    def _failure(error: str, *, action: Optional[str] = None) -> dict[str, Any]:
        result = {"success": False, "error": error}
        if action:
            result["action"] = action
        return result

    def _session(self, conversation_id: str) -> Optional[CommerceSession]:
        session = self._sessions.get(conversation_id)
        if session and time.monotonic() - session.created_at > self.session_ttl_seconds:
            self._sessions.pop(conversation_id, None)
            return None
        return session

    def _require_session(self, conversation_id: str) -> CommerceSession:
        session = self._session(conversation_id)
        if not session:
            raise AmazonCommerceError("The Amazon shopping session expired. Start a new search.")
        self._require_browser()
        return session

    def _require_browser(self) -> None:
        if self._browser is None:
            raise AmazonCommerceError("Chrome control is unavailable")
        required = ("open_tracked_chrome_tab", "get_chrome_tab_url", "execute_javascript_in_chrome_tab")
        if any(not hasattr(self._browser, name) for name in required):
            raise AmazonCommerceError("Chrome does not provide the required tracked-tab controls")

    async def _adopt_visible_amazon_search(self, conversation_id: str) -> Optional[CommerceSession]:
        """Recover only a foreground secure Amazon search after a reload.

        No tab is guessed, opened, or navigated here.  The caller's current
        Chrome tab must already be a secure Amazon.in search page.
        """
        self._require_browser()
        if not hasattr(self._browser, "get_active_chrome_tab"):
            return None
        active = await self._browser.get_active_chrome_tab()
        if not active.get("success"):
            return None
        tab_id = str(active.get("tab_id") or "")
        url = str(active.get("url") or "")
        parsed = urllib.parse.urlparse(url)
        if (
            not tab_id.isdigit()
            or parsed.scheme != "https"
            or parsed.hostname not in {"amazon.in", "www.amazon.in"}
            or not (parsed.path == "/s" or parsed.path.startswith("/s/"))
        ):
            return None
        query = urllib.parse.parse_qs(parsed.query).get("k", [""])[0]
        session = CommerceSession(
            conversation_id=conversation_id,
            tab_id=tab_id,
            max_price_inr=self.hard_total_cap,
            query=self._normalise_query(query or "Amazon results"),
        )
        self._sessions[conversation_id] = session
        try:
            await self._refresh_products(session, wait_for_page=False)
        except AmazonCommerceError:
            self._sessions.pop(conversation_id, None)
            return None
        return session

    async def _require_page(self, session: CommerceSession, expected: str, *, asin: Optional[str] = None) -> None:
        parsed = await self._require_secure_amazon_tab(session)
        path = parsed.path.lower()
        is_search = path == "/s" or path.startswith("/s/")
        is_product = bool(re.search(r"/(?:dp|gp/product)/[a-z0-9]{10}(?:/|$)", path))
        is_checkout = "checkout" in path or "/gp/buy" in path
        allowed = {"search": is_search, "product": is_product, "checkout": is_checkout}
        if not allowed.get(expected):
            raise AmazonCommerceError("Amazon showed an unexpected page, so the workflow stopped")
        if asin and asin.lower() not in path:
            raise AmazonCommerceError("The tracked product page does not match the selected product")

    async def _require_secure_amazon_tab(self, session: CommerceSession) -> urllib.parse.ParseResult:
        response = await self._browser.get_chrome_tab_url(session.tab_id)
        if not response.get("success"):
            raise AmazonCommerceError("The tracked Amazon tab is no longer available")
        url = str(response.get("url") or "")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"amazon.in", "www.amazon.in"}:
            raise AmazonCommerceError("The tracked tab is not a secure Amazon.in page")
        return parsed

    async def _refresh_products(self, session: CommerceSession, *, wait_for_page: bool) -> list[dict[str, Any]]:
        attempts = 3 if wait_for_page else 1
        for attempt in range(attempts):
            try:
                await self._require_page(session, "search")
                response = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, _READ_SEARCH_RESULTS)
                if response.get("success"):
                    self._update_product_snapshot(session, response.get("result"))
                    if session.observed_products:
                        return session.products
            except AmazonCommerceError:
                raise
            if attempt + 1 < attempts:
                await asyncio.sleep(0.6)
        return session.products

    def _update_product_snapshot(self, session: CommerceSession, raw: Any) -> None:
        """Update bounded Amazon card data without trusting it as instructions."""
        observed = self._decode_products(raw, 1_000_000, max_items=_MAX_OBSERVED_PRODUCTS)
        if observed:
            session.observed_products = observed
            session.products = [
                item for item in observed if item["price_inr"] <= session.max_price_inr
            ][:_MAX_PRODUCTS]

    async def _show_visible_product_numbers(self, session: CommerceSession) -> bool:
        """Add ephemeral local number badges to visible eligible result cards."""
        visible = self._visible_products(session)
        labels = {item["asin"]: index for index, item in enumerate(visible, start=1)}
        if not labels:
            return True
        script = _LABEL_VISIBLE_PRODUCTS % json.dumps(labels, sort_keys=True, separators=(",", ":"))
        response = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, script)
        payload = self._decode_json(response.get("result"))
        return bool(response.get("success") and payload.get("ok"))

    @staticmethod
    def _visible_products(session: CommerceSession) -> list[dict[str, Any]]:
        visible = [item for item in session.products if item.get("visible")]
        return [dict(item, visible_number=index) for index, item in enumerate(visible, start=1)]

    @staticmethod
    def _products_message(session: CommerceSession) -> str:
        if AmazonCommerceService._visible_products(session):
            return "Amazon search results are ready"
        if session.products:
            return "Amazon has eligible results, but none are currently visible. Ask me to scroll further."
        if session.observed_products:
            lowest_price = min(item["price_inr"] for item in session.observed_products)
            return (
                f"Amazon loaded {len(session.observed_products)} visible results, but none are within "
                f"your ₹{session.max_price_inr} limit. The lowest visible price is ₹{lowest_price}."
            )
        return "Amazon is still loading results; ask me to scroll or try again."

    @staticmethod
    def _scroll_script(direction: str) -> str:
        movements = {
            "up": "window.scrollBy(0, -Math.max(650, Math.floor(window.innerHeight * 0.85)));",
            "top": "window.scrollTo(0, 0);",
            "bottom": "window.scrollTo(0, document.body.scrollHeight);",
        }
        return movements[direction] + _READ_SEARCH_RESULTS

    def _decode_products(
        self, raw: Any, max_price_inr: int, *, max_items: int = _MAX_PRODUCTS,
    ) -> list[dict[str, Any]]:
        decoded = self._decode_json(raw)
        raw_products = decoded.get("products", []) if isinstance(decoded, dict) else []
        products: list[dict[str, Any]] = []
        for item in raw_products if isinstance(raw_products, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                asin = self._validate_asin(item.get("asin"))
                price = self._parse_inr(item.get("price"))
            except AmazonCommerceError:
                continue
            if price > max_price_inr:
                continue
            title = self._clean_text(item.get("title"), _TITLE_MAX)
            if not title:
                continue
            products.append({
                "asin": asin, "title": title, "price_inr": price,
                "rating": self._clean_text(item.get("rating"), 32),
                "seller": self._clean_text(item.get("seller"), 100),
                "visible": bool(item.get("visible", True)),
            })
            if len(products) >= max(1, int(max_items)):
                break
        return products

    async def _read_order_snapshot(self, session: CommerceSession, *, require_place_control: bool) -> dict[str, Any]:
        await self._require_page(session, "checkout")
        response = await self._browser.execute_javascript_in_chrome_tab(session.tab_id, _READ_CHECKOUT)
        if not response.get("success"):
            raise AmazonCommerceError("Could not re-read the Amazon checkout")
        raw = self._decode_json(response.get("result"))
        if require_place_control and not raw.get("place_order_available"):
            raise AmazonCommerceError("Amazon did not show a Place Order control; no order was prepared")
        if not session.selected:
            raise AmazonCommerceError("No selected Amazon product")
        item_price = self._parse_inr(raw.get("item_price"))
        delivery_fee = self._parse_delivery_fee(raw.get("delivery_fee"))
        total = self._parse_inr(raw.get("total"))
        if item_price != session.selected["price_inr"]:
            raise AmazonCommerceError("The Amazon item price changed; no order was prepared")
        if total != item_price + delivery_fee:
            raise AmazonCommerceError("The Amazon total could not be verified exactly")
        if total > min(session.max_price_inr, self.hard_total_cap):
            raise AmazonCommerceError("The Amazon total exceeds the requested or ₹500 safety cap")
        raw_address = self._clean_text(raw.get("address"), 300)
        if not raw_address:
            raise AmazonCommerceError("A default delivery address was not visible; the workflow stopped")
        address_hash = hashlib.sha256(raw_address.encode("utf-8")).hexdigest()[:16]
        snapshot = {
            "asin": session.selected["asin"],
            "title": session.selected["title"],
            "seller": self._clean_text(raw.get("seller"), 100) or session.selected.get("seller", ""),
            "quantity": 1,
            "payment_method": "cod",
            "item_price_inr": item_price,
            "delivery_fee_inr": delivery_fee,
            "total_inr": total,
            "address_id": f"addr_{address_hash}",
        }
        fingerprint_input = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        snapshot["checkout_fingerprint"] = hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()
        return snapshot

    @staticmethod
    def _normalise_snapshot(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AmazonCommerceError("Order snapshot is missing")
        expected = {
            "asin": str(value.get("asin") or ""),
            "title": str(value.get("title") or ""),
            "seller": str(value.get("seller") or ""),
            "quantity": int(value.get("quantity") or 0),
            "payment_method": str(value.get("payment_method") or "").lower(),
            "item_price_inr": int(value.get("item_price_inr") or 0),
            "delivery_fee_inr": int(value.get("delivery_fee_inr") or 0),
            "total_inr": int(value.get("total_inr") or 0),
            "address_id": str(value.get("address_id") or ""),
            "checkout_fingerprint": str(value.get("checkout_fingerprint") or ""),
        }
        if expected["quantity"] != 1 or expected["payment_method"] != "cod":
            raise AmazonCommerceError("Only one-item Cash on Delivery snapshots are permitted")
        if not _ASIN_RE.fullmatch(expected["asin"]):
            raise AmazonCommerceError("Order snapshot ASIN is invalid")
        verified = dict(expected)
        fingerprint = verified.pop("checkout_fingerprint")
        recomputed = hashlib.sha256(
            json.dumps(verified, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()
        if not fingerprint or fingerprint != recomputed:
            raise AmazonCommerceError("Order snapshot fingerprint is invalid")
        return expected

    async def _get_gate(self) -> str:
        store = self._store or get_state_store()
        try:
            raw = await store.get_runtime_value(_SESSION_KEY)
            data = json.loads(raw) if raw else {}
            state = data.get("state") if isinstance(data, dict) else None
            return state if state in {"rehearsal_required", "armed_one_live_order", "disarmed"} else "rehearsal_required"
        except Exception:
            # If we cannot durably audit/remember safety state, never dispatch.
            return "disarmed"

    async def _set_gate(self, state: str) -> None:
        store = self._store or get_state_store()
        await store.set_runtime_value(_SESSION_KEY, json.dumps({"state": state, "updated_at": int(time.time())}, separators=(",", ":")))

    @staticmethod
    def _parse_search_request(text: str) -> Optional[tuple[str, int]]:
        lowered = text.lower()
        # “Find lamps under ₹500” is a natural hands-free shopping request;
        # do not require the user to repeat “on Amazon” when the INR amount
        # already makes the commercial intent explicit.  Plain numeric research
        # requests remain outside this route unless they name Amazon.
        has_amazon = "amazon" in lowered
        has_inr_marker = bool(re.search(r"(?:₹|\brs\.?\b|\binr\b|\brupees?\b)", lowered))
        has_shopping_verb = bool(re.search(r"\b(?:open|find|search|look(?:\s+for)?|show|list)\b", lowered))
        if not has_shopping_verb or (not has_amazon and not has_inr_marker):
            return None
        price_match = re.search(r"\b(?:under|below|less than|upto|up to)\s*(?:₹|rs\.?|inr)?\s*([0-9][0-9,]*)\b", lowered)
        if not price_match:
            return None
        # Keep the item type, not conversational scaffolding.  For example,
        # "open a list of chairs under ₹500 on Amazon" becomes "chairs".
        prefix = text[:price_match.start()]
        prefix = re.split(r"\b(?:on|in)\s+amazon(?:\.in)?\b", prefix, maxsplit=1, flags=re.I)[0]
        query = re.sub(
            r"^\s*(?:please\s+)?(?:(?:open|find|search|show|look(?:\s+for)?)\s+)?"
            r"(?:(?:a|the)\s+)?(?:(?:list|options?|products?|items?)\s+(?:of\s+)?)?",
            "", prefix, flags=re.I,
        )
        query = query.strip(" .,:-\t")
        if not query:
            return None
        try:
            return query, int(price_match.group(1).replace(",", ""))
        except ValueError:
            return None

    @staticmethod
    def _parse_open_request(text: str) -> Optional[str]:
        # Accept polite hands-free forms, and recover the common ASR rendering
        # of "open" as "of one".  This only runs inside a bounded Amazon
        # session; it can never become a generic application launch.
        normalized = re.sub(r"^\s*of\s+one\s+", "open ", str(text or ""), flags=re.I)
        match = re.match(
            r"^\s*(?:(?:can|could|would|will)\s+you\s+|please\s+)?(?:open|select|choose)\s+(.+?)\s*[.?!]*\s*$",
            normalized,
            flags=re.I,
        )
        if not match:
            return None
        request = match.group(1).strip()
        return re.sub(
            r"\s+(?:at|on|from)\s+(?:(?:the|this)\s+)?(?:top|bottom|middle)"
            r"(?:\s+of\s+(?:(?:the|this)\s+)?(?:page|screen|results?))?\s*$",
            "", request, flags=re.I,
        ).strip() or None

    @staticmethod
    def _is_cart_request(text: str) -> bool:
        return bool(re.fullmatch(
            r"\s*(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:open|show|go\s+to)\s+(?:(?:my|the)\s+)?"
            r"(?:cart|carts|card|cards)(?:\s+(?:from|on|in)\s+amazon(?:\.in)?)?\s*[.?!]*\s*",
            text,
            flags=re.I,
        ))

    @staticmethod
    def _is_add_to_cart_request(text: str) -> bool:
        return bool(re.search(r"\badd\b.{0,40}\b(?:to\s+)?(?:cart|card|cut)\b", text, flags=re.I))

    @staticmethod
    def _is_buy_request(text: str) -> bool:
        return bool(re.fullmatch(
            r"\s*(?:(?:i\s+)?(?:want|would\s+like)\s+to\s+|(?:let'?s|please|can\s+you|could\s+you)\s+)?"
            r"(?:buy|by|purchase|checkout|check\s+out)(?:\s+(?:it|this|that))?(?:\s+(?:product|item))?\s*[.?!]*\s*",
            text,
            flags=re.I,
        ))

    @staticmethod
    def _parse_scroll_direction(text: str) -> Optional[str]:
        """Recognize a bounded set of natural scroll follow-ups."""
        normalized = re.sub(r"\s+", " ", str(text or "").lower()).strip()
        # Speech often supplies a polite wrapper. Strip only this fixed prefix,
        # not arbitrary prose, so browser control remains deterministic.
        normalized = re.sub(
            r"^(?:(?:can|could|would|will)\s+(?:you|it|we)\s+)?(?:please\s+)?",
            "", normalized,
        ).strip()
        if normalized in {"next results", "more results"}:
            return "down"
        if not normalized.startswith("scroll"):
            return None
        remainder = normalized[len("scroll"):].strip()
        allowed_words = {"further", "a", "bit", "more", "again", "down", "up", "to", "top", "bottom"}
        words = re.findall(r"[a-z]+", remainder)
        if any(word not in allowed_words for word in words):
            return None
        if not remainder or not words:
            return "down"
        if "top" in words:
            return "top"
        if "bottom" in words:
            return "bottom"
        if "up" in words:
            return "up"
        return "down"

    def _resolve_product(self, session: CommerceSession, request: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
        # Strip harmless spoken context, e.g. "open the first option on this
        # screen".  The selection still resolves only against this session's
        # captured, price-qualified results.
        request = re.sub(
            r"\s+(?:on|from)\s+(?:(?:this|the)\s+)?(?:screen|page|results?)\s*$",
            "", request, flags=re.I,
        ).strip()
        # The local overlay calls cards “Item 1”, “Item 2”, etc. Treat
        # that spoken label exactly like its visible ordinal, not as product
        # text.  This remains bounded to the current visible result set.
        request = re.sub(r"^(?:the\s+)?(?:item|number)\s+", "", request, flags=re.I).strip()
        selectable = self._visible_products(session)
        ordinal = re.fullmatch(
            r"(?:the\s+)?(first|second|third|fourth|fifth|\d+)(?:\s+(?:one|item|product|option|result))?",
            request, flags=re.I,
        )
        if ordinal:
            labels = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5}
            token = ordinal.group(1).lower()
            index = labels.get(token, int(token) if token.isdigit() else 0) - 1
            if 0 <= index < len(selectable):
                return selectable[index], None
            return None, "That numbered product is not currently visible in the Amazon results."
        needle = self._clean_text(request, 100).lower()
        # A spoken product name often omits marketing words in Amazon's title.
        # For example, "HP Z3700 mouse" must match "HP Z3700 Wireless Mouse".
        # Resolve only when every meaningful requested token occurs in exactly
        # one current, price-qualified result.
        needle_tokens = self._title_tokens(needle)
        matches = [
            product for product in selectable
            if needle and (
                needle in product["title"].lower()
                or (needle_tokens and needle_tokens.issubset(self._title_tokens(product["title"])))
            )
        ]
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, "More than one visible product matches that name. Please say its number, such as ‘open the second one’."
        fuzzy_matches = self._fuzzy_product_matches(selectable, needle_tokens)
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0], None
        if len(fuzzy_matches) > 1:
            return None, "More than one visible product sounds like that name. Please say its number, such as ‘open the second one’."
        eligible_matches = [
            product for product in session.products
            if needle and (
                needle in product["title"].lower()
                or (needle_tokens and needle_tokens.issubset(self._title_tokens(product["title"])))
            )
        ]
        if len(eligible_matches) == 1:
            return None, "I found that eligible item, but it is not visible on the screen now. Ask me to scroll to it first."
        observed_matches = [
            product for product in session.observed_products
            if needle and (
                needle in product["title"].lower()
                or (needle_tokens and needle_tokens.issubset(self._title_tokens(product["title"])))
            )
        ]
        if len(observed_matches) == 1:
            price = observed_matches[0]["price_inr"]
            return None, (
                f"That product is listed at ₹{price}, which exceeds your ₹{session.max_price_inr} limit. "
                "I will not open it in this capped test."
            )
        if not session.products and session.observed_products:
            return None, (
                f"None of the visible Amazon results are within your ₹{session.max_price_inr} limit, "
                "so there is no eligible product to open."
            )
        return None, "I could not uniquely match that product in the current visible Amazon results."

    async def _verify_screen_visible_product(
        self, session: CommerceSession, product: dict[str, Any],
    ) -> dict[str, Any]:
        """Require visual evidence before an Amazon product can be opened.

        The Chrome tab remains ASIN-pinned and the final navigation is still a
        code-owned canonical URL; no coordinates or untrusted selectors are
        ever clicked.
        """
        if not self.requires_screen_observation:
            return {"success": True, "screen_verified": False}
        if not hasattr(self._browser, "activate_tracked_chrome_tab"):
            return {"success": False, "error": "Chrome cannot bring the tracked product list into view"}
        try:
            activated = await asyncio.wait_for(
                self._browser.activate_tracked_chrome_tab(session.tab_id), timeout=6,
            )
        except asyncio.TimeoutError:
            return {"success": False, "error": "Chrome did not bring the tracked tab forward in time"}
        if not activated.get("success"):
            return {"success": False, "error": "Could not bring the tracked Amazon tab to the screen"}
        await asyncio.sleep(0.2)
        observer = self._screen_observer
        if observer is None:
            from mac.screen_observer import ScreenObserver
            observer = ScreenObserver()
            self._screen_observer = observer
        try:
            verified = await asyncio.wait_for(
                observer.verify_product_label(str(product.get("title") or "")), timeout=12,
            )
        except asyncio.TimeoutError:
            return {"success": False, "error": "Screen observation timed out; no product was opened"}
        if not verified.get("success"):
            return {"success": False, "error": "Screen observation is unavailable. Grant macOS Screen Recording permission, then try again."}
        if not verified.get("matched"):
            return {"success": False, "error": "The selected product label is not visible on the screen, so I will not open it."}
        return {"success": True, "screen_verified": True}

    @staticmethod
    def _title_tokens(value: Any) -> set[str]:
        """Produce comparison-only product tokens; never use page text as code."""
        tokens = {
            token for token in re.findall(r"[a-z0-9]+", str(value or "").lower())
            if len(token) >= 2
        }
        # These are product-language normalisations, not instructions from the
        # page.  They make ordinary speech such as “ultra soft bed sheet” map
        # to Amazon's often-concatenated marketing spellings.
        for token in tuple(tokens):
            if token == "bedsheet":
                tokens.update({"bed", "sheet"})
            elif token == "ultrasoft":
                tokens.update({"ultra", "soft"})
            elif token in {"cc", "tcc"}:
                tokens.add("tc")
        return tokens

    @classmethod
    def _fuzzy_product_matches(cls, products: list[dict[str, Any]], requested: set[str]) -> list[dict[str, Any]]:
        """Return one safe spoken-title match, otherwise require clarification.

        Fuzzy matching is deliberately bounded to the currently visible,
        price-qualified cards and needs several independent token matches.
        It handles speech-to-text drift (for example Rivermour/Rivermoor),
        rather than guessing from a single generic word such as “bedsheet”.
        """
        ignored = {"the", "and", "for", "with", "from", "this", "that", "at", "top", "bottom", "page", "screen", "product", "item", "one", "a", "an"}
        requested = {token for token in requested if token not in ignored}
        if len(requested) < 2:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for product in products:
            title_tokens = cls._title_tokens(product.get("title"))
            score = 0.0
            exact = 0
            for token in requested:
                if token in title_tokens:
                    exact += 1
                    score += 1.35 if any(char.isdigit() for char in token) else 1.0
                    continue
                if len(token) < 4:
                    continue
                nearest = max((difflib.SequenceMatcher(None, token, candidate).ratio() for candidate in title_tokens), default=0.0)
                if nearest >= 0.84:
                    score += 0.8
            coverage = score / max(1, len(requested))
            if exact >= 2 and score >= 3.0 and coverage >= 0.55:
                scored.append((score, product))
        if not scored:
            return []
        scored.sort(key=lambda entry: entry[0], reverse=True)
        if len(scored) == 1:
            return [scored[0][1]]
        # Similar alternatives need a material confidence lead; otherwise the
        # user gets an ordinal clarification instead of an accidental open.
        if scored[0][0] - scored[1][0] >= 1.25:
            return [scored[0][1]]
        return [product for _, product in scored]

    def _normalise_query(self, query: Any) -> str:
        cleaned = self._normalise_spoken_commerce_text(self._clean_text(query, 140))
        if not cleaned:
            raise AmazonCommerceError("Amazon search needs a product name")
        return cleaned

    def _normalise_limit(self, value: Any) -> int:
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise AmazonCommerceError("Amazon search needs a valid INR price limit")
        if not 1 <= limit <= self.hard_total_cap:
            raise AmazonCommerceError(f"This test only supports an INR limit from 1 to ₹{self.hard_total_cap}")
        return limit

    @staticmethod
    def _normalise_spoken_commerce_text(value: str) -> str:
        """Correct known, low-risk shopping homophones without global rewriting."""
        text = str(value or "")
        # Keep this list short and evidence-driven. It is used only for product
        # search/matching and never changes the raw voice transcript.
        return re.sub(r"\b(?:big|bed)\s+(?:shit|sheet)\b", "bedsheet", text, flags=re.I)

    @staticmethod
    def _validate_asin(value: Any) -> str:
        asin = str(value or "").upper().strip()
        if not _ASIN_RE.fullmatch(asin):
            raise AmazonCommerceError("Amazon product identifier is invalid")
        return asin

    @staticmethod
    def _clean_text(value: Any, maximum: int) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]

    @staticmethod
    def _decode_json(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            decoded = json.loads(str(value or "{}"))
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError):
            return {}

    @staticmethod
    def _parse_inr(value: Any) -> int:
        match = _PRICE_RE.search(str(value or ""))
        if not match:
            raise AmazonCommerceError("Amazon did not show a valid INR amount")
        try:
            amount = float(match.group(1).replace(",", ""))
        except ValueError:
            raise AmazonCommerceError("Amazon showed an invalid INR amount")
        if amount < 0 or amount > 1_000_000:
            raise AmazonCommerceError("Amazon amount is outside the allowed range")
        return int(round(amount))

    def _parse_delivery_fee(self, value: Any) -> int:
        if "free" in str(value or "").lower():
            return 0
        return self._parse_inr(value)

    @staticmethod
    def _mask_order_reference(value: Any) -> str:
        raw = re.sub(r"[^0-9-]", "", str(value or ""))
        return ("…" + raw[-4:]) if len(raw) >= 4 else "masked"

    @staticmethod
    def _snapshot_message(snapshot: dict[str, Any]) -> str:
        return (
            f"COD preview ready: {snapshot['title']} from {snapshot['seller'] or 'the listed seller'}, "
            f"item ₹{snapshot['item_price_inr']}, delivery ₹{snapshot['delivery_fee_inr']}, "
            f"total ₹{snapshot['total_inr']}. Say ‘place order’ to prepare the required admin activation."
        )


_service: Optional[AmazonCommerceService] = None


def get_amazon_commerce_service() -> AmazonCommerceService:
    global _service
    if _service is None:
        _service = AmazonCommerceService()
    return _service
