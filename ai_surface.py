"""Track how AI assistants price/represent your products — a complement to PDP scraping.

In 2026, buyers increasingly get prices from AI assistants, not only product pages. This
asks ScrapingBee's ChatGPT endpoint (with live web search) what an AI tells a shopper about
your product right now, so you can monitor your "AI-surface" pricing over time alongside the
real PDP prices from tracker.py — and catch when the AI quotes you wrong or stale.

  python ai_surface.py "Logitech M185 wireless mouse" us

Note: ChatGPT-with-search is heavy (LLM + live web search, ~15 credits) and can be slow or
504 on big queries — run it daily/weekly, not on a 6-hour cron like the price tracker.
"""
import os
import sys

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.environ.get("SCRAPINGBEE_API_KEY")
if not API_KEY:
    sys.exit("Set SCRAPINGBEE_API_KEY (export it or put it in .env).")


def ai_surface(product: str, country: str = "us") -> str:
    """Return what an AI assistant tells a shopper about this product's price right now."""
    r = requests.get(
        "https://app.scrapingbee.com/api/v1/chatgpt",
        headers={"Authorization": f"Bearer {API_KEY}"},
        params={
            "prompt": f"What is the current price of the {product} and which retailer is cheapest?",
            "search": "true",         # use live web data, not the model's training cutoff
            "country_code": country,   # answers are geo-sensitive — set your market
        },
        timeout=120,
    )
    r.raise_for_status()
    return r.json().get("results_text", "")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit('usage: python ai_surface.py "<product>" [country_code]')
    print(ai_surface(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "us"))
