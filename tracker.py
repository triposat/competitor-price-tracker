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
import re
import sys
from concurrent.futures import ThreadPoolExecutor
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
OUR_CURRENCY = os.environ.get("OUR_CURRENCY", "USD")  # we compare prices only in this currency (no FX)
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "8"))  # concurrent fetches; keep <= your plan's concurrency cap

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


def _parse_price_text(text: str) -> tuple[float | None, str | None]:
    """Pull a number + currency out of a price string like '£51.77' or '1.299,00 €'."""
    currency = next((c for s, c in CURRENCY_SYMBOLS.items() if s in text), None)
    m = re.search(r"\d[\d.,\s]*\d|\d", text)
    if not m:
        return None, currency
    raw = m.group(0).replace(" ", "")
    if "," in raw and "." in raw:                # 1.299,00 or 1,299.00 — last separator is the decimal
        raw = raw.replace(".", "").replace(",", ".") if raw.rfind(",") > raw.rfind(".") else raw.replace(",", "")
    elif "," in raw:                             # comma is decimal only if exactly 2 trailing digits
        raw = raw.replace(",", ".") if re.search(r",\d{2}$", raw) else raw.replace(",", "")
    try:
        return float(raw), currency
    except ValueError:
        return None, currency


def _css_price(html: str, selector: str) -> dict[str, Any] | None:
    """Per-site override: read the price straight from a CSS selector you supply."""
    el = BeautifulSoup(html, "html.parser").select_one(selector)
    if not el:
        return None
    price, currency = _parse_price_text(el.get_text(" ", strip=True))
    return None if price is None else {"price": price, "currency": currency, "in_stock": True}


def _meta_price(html: str) -> dict[str, Any] | None:
    """OpenGraph / microdata price meta — present on many sites that lack JSON-LD."""
    soup = BeautifulSoup(html, "html.parser")

    def meta(*keys: str) -> str | None:
        for k in keys:
            el = soup.find("meta", property=k) or soup.find("meta", attrs={"name": k})
            if el and el.get("content"):
                return el["content"]
        return None

    amount = meta("product:price:amount", "og:price:amount")
    if not amount:
        el = soup.find(attrs={"itemprop": "price"})
        amount = (el.get("content") or el.get_text(strip=True)) if el else None
    if not amount:
        return None
    price, sym_cur = _parse_price_text(str(amount))
    if price is None:
        return None
    avail = meta("product:availability", "og:availability") or ""
    return {"price": price,
            "currency": meta("product:price:currency", "og:price:currency") or sym_cur,
            "in_stock": "out" not in avail.lower()}


def _proxy_params(proxy: str | None) -> dict[str, str]:
    """Per-target proxy tier for protected sites. classic=1 credit (no JS); premium=25
    (residential + JS, for Cloudflare/DataDome); stealth=75 (hardest anti-bot, JS forced)."""
    if proxy == "premium":
        return {"premium_proxy": "true", "render_js": "true"}
    if proxy == "stealth":
        return {"stealth_proxy": "true"}        # stealth forces JS on
    return {"render_js": "false"}               # classic: cheap deterministic pass


def fetch_generic(s: requests.Session, url: str, selector: str | None = None,
                  proxy: str | None = None, attempts: int = 3) -> dict[str, Any]:
    # Deterministic pass (classic=1 credit; premium/stealth fetch with JS so the cascade
    # works on protected, JS-rendered pages too).
    r = s.get(HTML_API, params={"url": url, **_proxy_params(proxy)}, timeout=120)
    _check_auth(r)
    html = r.text if r.ok else ""
    if html:
        if selector and (data := _css_price(html, selector)):   # 1. your per-site override
            return data
        if (data := _jsonld_price(html)):                       # 2. schema.org/Product JSON-LD
            return data
        if (data := _meta_price(html)):                         # 3. OpenGraph / microdata meta
            return data
    # 4. AI extraction — last resort (flaky; can 200 with a non-JSON "Sorry..." body, still billed)
    ai = {"url": url, "ai_extract_rules": json.dumps(AI_RULES)}
    if proxy in ("premium", "stealth"):
        ai[f"{proxy}_proxy"] = "true"
    for _ in range(attempts):
        r = s.get(HTML_API, params=ai, timeout=120)
        _check_auth(r)
        try:
            d = r.json()
        except ValueError:
            continue
        if d.get("price") is not None:
            return {"price": d["price"], "currency": d.get("currency"), "in_stock": d.get("in_stock")}
    raise RuntimeError(f"no price from {url} (selector/JSON-LD/meta + {attempts} AI attempts)")


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


def _fetch_target(session: requests.Session, t: dict) -> tuple[dict | None, str | None]:
    """Fetch one target. Returns (data, None) or (None, error) — safe to run in the pool."""
    try:
        if t["source"] == "generic":
            return fetch_generic(session, t["identifier"],
                                 selector=(t.get("selector") or None),
                                 proxy=(t.get("proxy") or None)), None
        return DISPATCH[t["source"]](session, t["identifier"]), None
    except (requests.RequestException, RuntimeError) as e:
        return None, str(e)


def track(targets: str = "targets.csv", history: str = "history.csv") -> list[PriceSnapshot]:
    session = build_session()
    last_state = load_last_state(history)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    snapshots: list[PriceSnapshot] = []

    with open(targets, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Fetch concurrently (network-bound); keep MAX_WORKERS <= your plan's concurrency cap.
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        results = list(pool.map(lambda t: _fetch_target(session, t), rows))

    # Process in row order so alerts and history stay deterministic.
    for t, (data, err) in zip(rows, results):
        sku = t["our_sku"]
        if err:
            print(f"  ! {sku}: fetch failed ({err})")
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

        # only compare prices in the same currency — this tracker does NOT do FX conversion,
        # so a EUR competitor vs a USD price is flagged and skipped, not silently mis-compared.
        currency_ok = (snap.currency or OUR_CURRENCY) == OUR_CURRENCY
        now_uc = currency_ok and is_undercut(comp, our_price)
        prev_uc = bool(prev) and currency_ok and is_undercut(prev[0], prev[1])
        if snap.in_stock and now_uc and (not prev_uc or comp < prev[0] - 0.01):
            pct = (our_price - comp) / our_price * 100
            send_alert(f":rotating_light: {sku}: {snap.competitor} {snap.currency} {comp} "
                       f"vs our {our_price} ({pct:.1f}% lower)")

        if not currency_ok:
            note = f"  [currency {snap.currency}≠{OUR_CURRENCY}, not compared]"
        elif now_uc:
            note = f"  <-- UNDERCUT {((our_price - comp) / our_price * 100):.1f}%"
        else:
            note = ""
        stock = "" if snap.in_stock else " [OUT OF STOCK]"
        print(f"  {sku:12} {snap.competitor:8} {snap.currency} {comp} (ours {our_price}){note}{stock}")

    if not snapshots:
        print("No snapshots captured.")
        return snapshots

    write_header = not os.path.exists(history)
    with open(history, "a", newline="", encoding="utf-8") as f:
        # lineterminator="\n" so appended rows match the LF header — a mixed LF/CRLF file
        # breaks DuckDB's CSV sniffer (and the MCP/analytics path that reads it)
        writer = csv.DictWriter(f, fieldnames=[fld.name for fld in fields(PriceSnapshot)], lineterminator="\n")
        if write_header:
            writer.writeheader()
        writer.writerows(asdict(s) for s in snapshots)
    return snapshots


if __name__ == "__main__":
    print("Running competitor price tracker...\n")
    track()
