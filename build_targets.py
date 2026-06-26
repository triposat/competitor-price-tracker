"""Build (or extend) targets.csv from a keyword search + SKU matching.

Matching is a *setup-time* step, not a per-run one: you decide which competitor
products are yours once, then track those IDs. This runs an Amazon/Walmart search,
matches each result against your product with matcher.py, and prints the targets.csv
rows you should keep (with a confidence tag).

  python build_targets.py "Logitech M185 Wireless Mouse" "logitech wireless mouse" 14.99
"""
import os
import sys

import requests

from matcher import match

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.environ.get("SCRAPINGBEE_API_KEY")
if not API_KEY:
    sys.exit("Set SCRAPINGBEE_API_KEY (export it or put it in .env).")

SEARCH = {"amazon": "amazon/search", "walmart": "walmart/search"}


def suggest(our_title: str, keyword: str, our_price: str, source: str = "amazon", limit: int = 10) -> None:
    r = requests.get(f"https://app.scrapingbee.com/api/v1/{SEARCH[source]}",
                     headers={"Authorization": f"Bearer {API_KEY}"},
                     params={"query": keyword}, timeout=90)
    seen = set()
    print(f"# suggested targets.csv rows for {our_title!r} (source={source})")
    for p in r.json().get("products", [])[:limit]:
        ident = str(p.get("asin") or p.get("id") or "")
        title = p.get("title", "")
        if not ident or ident in seen:
            continue
        seen.add(ident)
        res = match({"title": our_title}, {"title": title})
        if not res.matched:
            continue
        sku = our_title.split()[0].upper() + "-" + ident[:6]
        tag = "review" if res.needs_review else "ok"
        print(f"{sku},{source},{ident},{our_price}    # conf={res.confidence} via {res.method} [{tag}] {title[:48]}")


if __name__ == "__main__":
    if len(sys.argv) < 4:
        sys.exit('usage: python build_targets.py "<your product title>" "<search keyword>" <your_price> [amazon|walmart]')
    our_title, keyword, our_price = sys.argv[1], sys.argv[2], sys.argv[3]
    source = sys.argv[4] if len(sys.argv) > 4 else "amazon"
    suggest(our_title, keyword, our_price, source)
