"""Competitor price tracker — fetch, normalize, store, alert.

Reads targets.csv, dispatches each row to the right ScrapingBee endpoint, normalizes
every response into one typed PriceSnapshot, appends to history.csv, and posts a Slack
alert when a competitor undercuts your price by THRESHOLD. Built from individually
tested pieces; run end-to-end with `python tracker.py`.
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, asdict, fields
from datetime import datetime, timezone
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

API_KEY = os.environ["SCRAPINGBEE_API_KEY"]
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
    """Map a bare symbol ('$') to an ISO code ('USD'); AI extraction sometimes returns symbols."""
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


def fetch_amazon(s: requests.Session, asin: str) -> dict[str, Any]:
    d = s.get(AMAZON_API, params={"query": asin}, timeout=90).json()
    return {"price": d.get("price"), "currency": d.get("currency"),
            "in_stock": d.get("stock") not in (None, "", "Currently unavailable")}


def fetch_walmart(s: requests.Session, item_id: str) -> dict[str, Any]:
    d = s.get(WALMART_API, params={"product_id": item_id}, timeout=90).json()
    return {"price": d.get("price"), "currency": d.get("currency"),
            "in_stock": not d.get("out_of_stock", False)}


def fetch_generic(s: requests.Session, url: str, attempts: int = 5) -> dict[str, Any]:
    # AI extraction can 200 with "Sorry, couldn't get the response from AI" (not JSON) and still bill.
    for _ in range(attempts):
        r = s.get(HTML_API, params={"url": url, "ai_extract_rules": json.dumps(AI_RULES)}, timeout=120)
        try:
            d = r.json()
        except ValueError:
            continue
        return {"price": d.get("price"), "currency": d.get("currency"), "in_stock": d.get("in_stock")}
    raise RuntimeError(f"AI extraction failed for {url} after {attempts} attempts")


DISPATCH: dict[str, Callable[[requests.Session, str], dict[str, Any]]] = {
    "amazon": fetch_amazon, "walmart": fetch_walmart, "generic": fetch_generic,
}


def load_last_prices(history: str) -> dict[str, float]:
    """Latest stored price per SKU, for the drift sanity check."""
    last: dict[str, float] = {}
    if not os.path.exists(history):
        return last
    with open(history, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("comp_price"):
                last[row["our_sku"]] = float(row["comp_price"])
    return last


def send_alert(text: str) -> None:
    if SLACK_WEBHOOK_URL:
        requests.post(SLACK_WEBHOOK_URL, json={"text": text}, timeout=10)
    else:
        print("ALERT:", text)


def track(targets: str = "targets.csv", history: str = "history.csv") -> list[PriceSnapshot]:
    session = build_session()
    last_prices = load_last_prices(history)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshots: list[PriceSnapshot] = []

    with open(targets, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for t in rows:
        try:
            data = DISPATCH[t["source"]](session, t["identifier"])
        except (requests.RequestException, RuntimeError) as e:
            print(f"  ! {t['our_sku']}: fetch failed ({e})")
            continue

        comp = data["price"]
        our_price = float(t["our_price"])

        # sanity checks: a None price or a >50% jump from last time is usually a break, not a real move
        prev = last_prices.get(t["our_sku"])
        if comp is None:
            print(f"  ? {t['our_sku']}: no price extracted — skipping (check the target/selector)")
            continue
        if prev and abs(comp - prev) > 0.5 * prev:
            print(f"  ? {t['our_sku']}: price moved >50% ({prev} -> {comp}) — verify before trusting")

        snap = PriceSnapshot(now, t["our_sku"], t["source"], float(comp),
                             normalize_currency(data["currency"]), bool(data["in_stock"]), our_price)
        snapshots.append(snap)

        flag = ""
        if comp < our_price * (1 - THRESHOLD):
            pct = (our_price - comp) / our_price * 100
            flag = f"  <-- UNDERCUT by {pct:.1f}%"
            send_alert(f":rotating_light: {snap.our_sku}: {snap.competitor} {snap.currency} {comp} "
                       f"vs our {our_price} ({pct:.1f}% lower)")
        print(f"  {snap.our_sku:12} {snap.competitor:8} {snap.currency} {comp} (ours {our_price}){flag}")

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
