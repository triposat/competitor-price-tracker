"""Competitor price tracker — fetch, normalize, store, alert (on change).

Reads targets.csv, dispatches each row to the right ScrapingBee endpoint, normalizes
every response into one typed PriceSnapshot, appends to history.csv, and posts a Slack
alert ONLY when a competitor newly undercuts you (or drops further) while in stock.

The generic (non-marketplace) path tries JSON-LD first — deterministic and 1 credit —
and only falls back to AI extraction if the page has no usable structured data.
Run end-to-end with `python tracker.py`.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from dataclasses import dataclass, asdict, fields
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

if sys.version_info < (3, 10):
    sys.exit("This tracker requires Python 3.10+.")

try:                                   # load a local .env if python-dotenv is installed
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    API_KEY = os.environ["SCRAPINGBEE_API_KEY"]
except KeyError:
    sys.exit("Set SCRAPINGBEE_API_KEY before running (export it or put it in .env).")

SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")  # optional; prints if unset
THRESHOLD = float(os.environ.get("UNDERCUT_THRESHOLD", "0.05"))

HTML_API = "https://app.scrapingbee.com/api/v1/"
AMAZON_API = "https://app.scrapingbee.com/api/v1/amazon/product"
WALMART_API = "https://app.scrapingbee.com/api/v1/walmart/product"

AI_RULES = {
    "price": {"description": "current product price as a number", "type": "number"},
    "currency": {"description": "ISO 4217 currency code", "type": "string"},
    "in_stock": {"description": "whether the product is in stock", "type": "boolean"},
}
CURRENCY_SYMBOLS = {"$": "USD", "£": "GBP", "€": "EUR", "₹": "INR", "¥": "JPY"}


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    timestamp: str
    our_sku: str
    competitor: str
    comp_price: float | None
    currency: str | None
    in_stock: bool
    our_price: float


def normalize_currency(code: str | None) -> str | None:
    if not code:
        return None
    code = code.strip()
    return CURRENCY_SYMBOLS.get(code, code.upper()[:3] if code.isalpha() else code)


def build_session() -> requests.Session:
    retry = Retry(total=3, backoff_factor=1.0,
                  status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    s = requests.Session()
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers["Authorization"] = f"Bearer {API_KEY}"
    return s


def _check_auth(r: requests.Response) -> None:
    if r.status_code == 401:
        sys.exit("ScrapingBee returned 401 — check SCRAPINGBEE_API_KEY.")


def fetch_amazon(s: requests.Session, asin: str) -> dict[str, Any]:
    r = s.get(AMAZON_API, params={"query": asin}, timeout=90)
    _check_auth(r)
    d = r.json()
    return {"price": d.get("price"), "currency": d.get("currency"),
            "in_stock": d.get("stock") not in (None, "", "Currently unavailable")}


def fetch_walmart(s: requests.Session, item_id: str) -> dict[str, Any]:
    r = s.get(WALMART_API, params={"product_id": item_id}, timeout=90)
    _check_auth(r)
    d = r.json()
    return {"price": d.get("price"), "currency": d.get("currency"),
            "in_stock": not d.get("out_of_stock", False)}


def _find_product(node: Any) -> dict | None:
    if isinstance(node, dict):
        if node.get("@type") == "Product":
            return node
        for v in node.values():
            if (found := _find_product(v)):
                return found
    elif isinstance(node, list):
        for v in node:
            if (found := _find_product(v)):
                return found
    return None


def _jsonld_price(html: str) -> dict[str, Any] | None:
    """Deterministic extraction from a schema.org/Product JSON-LD block, if present."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        prod = _find_product(data)
        if not prod:
            continue
        offers = prod.get("offers")
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        offers = offers or {}
        price = offers.get("price")
        if price in (None, ""):
            continue
        avail = str(offers.get("availability") or "").lower()
        return {"price": float(str(price).replace(",", "")),
                "currency": offers.get("priceCurrency"),
                "in_stock": "instock" in avail}
    return None


def fetch_generic(s: requests.Session, url: str, attempts: int = 3) -> dict[str, Any]:
    # 1. JSON-LD first — deterministic, 1 credit, no JS
    r = s.get(HTML_API, params={"url": url, "render_js": "false"}, timeout=90)
    _check_auth(r)
    if r.ok and (data := _jsonld_price(r.text)):
        return data
    # 2. Fall back to AI extraction (can 200 with a non-JSON "Sorry..." body, still billed)
    for _ in range(attempts):
        r = s.get(HTML_API, params={"url": url, "ai_extract_rules": json.dumps(AI_RULES)}, timeout=120)
        _check_auth(r)
        try:
            d = r.json()
        except ValueError:
            continue
        if d.get("price") is not None:
            return {"price": d["price"], "currency": d.get("currency"), "in_stock": d.get("in_stock")}
    raise RuntimeError(f"no price from {url} (JSON-LD + {attempts} AI attempts)")


DISPATCH: dict[str, Callable[[requests.Session, str], dict[str, Any]]] = {
    "amazon": fetch_amazon, "walmart": fetch_walmart, "generic": fetch_generic,
}


def load_last_state(history: str) -> dict[str, tuple[float, float]]:
    """{sku: (comp_price, our_price)} from the most recent row per SKU — for change detection."""
    state: dict[str, tuple[float, float]] = {}
    if os.path.exists(history):
        with open(history, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("comp_price"):
                    state[row["our_sku"]] = (float(row["comp_price"]), float(row["our_price"]))
    return state


def is_undercut(comp: float | None, our: float) -> bool:
    return comp is not None and comp < our * (1 - THRESHOLD)


def send_alert(text: str) -> None:
    if SLACK_WEBHOOK_URL:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    else:
        print("ALERT:", text)


def track(targets: str = "targets.csv", history: str = "history.csv") -> list[PriceSnapshot]:
    session = build_session()
    last_state = load_last_state(history)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshots: list[PriceSnapshot] = []

    with open(targets, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for t in rows:
        sku = t["our_sku"]
        try:
            data = DISPATCH[t["source"]](session, t["identifier"])
        except (requests.RequestException, RuntimeError) as e:
            print(f"  ! {sku}: fetch failed ({e})")
            continue

        comp, our_price = data["price"], float(t["our_price"])
        prev = last_state.get(sku)

        if comp is None:
            print(f"  ? {sku}: no price extracted — skipping")
            continue
        if prev and abs(comp - prev[0]) > 0.5 * prev[0]:
            print(f"  ? {sku}: price moved >50% ({prev[0]} -> {comp}) — verify before trusting")

        snap = PriceSnapshot(now, sku, t["source"], float(comp),
                             normalize_currency(data["currency"]), bool(data["in_stock"]), our_price)
        snapshots.append(snap)

        # alert only on a NEW undercut (or a further drop) and only if the competitor is in stock
        now_uc = is_undercut(comp, our_price)
        prev_uc = is_undercut(prev[0], prev[1]) if prev else False
        if snap.in_stock and now_uc and (not prev_uc or comp < prev[0] - 0.01):
            pct = (our_price - comp) / our_price * 100
            send_alert(f":rotating_light: {sku}: {snap.competitor} {snap.currency} {comp} "
                       f"vs our {our_price} ({pct:.1f}% lower)")

        flag = f"  <-- UNDERCUT {((our_price - comp) / our_price * 100):.1f}%" if now_uc else ""
        stock = "" if snap.in_stock else " [OUT OF STOCK]"
        print(f"  {sku:12} {snap.competitor:8} {snap.currency} {comp} (ours {our_price}){flag}{stock}")

    if not snapshots:
        print("No snapshots captured.")
        return snapshots

    write_header = not os.path.exists(history)
    with open(history, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=[fld.name for fld in fields(PriceSnapshot)])
        if write_header:
            writer.writeheader()
        writer.writerows(asdict(s) for s in snapshots)
    return snapshots


if __name__ == "__main__":
    print("Running competitor price tracker...\n")
    track()
