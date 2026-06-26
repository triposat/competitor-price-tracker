# Competitor Price Tracker

A small, working competitor-price tracker built on the [ScrapingBee](https://www.scrapingbee.com/) API. Companion to the article *How to Track Competitor Prices Using Web Scraping*. Clone it, point it at your competitors, and get a Slack ping when one undercuts you.

It fetches Amazon and Walmart via ScrapingBee's dedicated parsers and any other retailer via the HTML API with AI extraction, normalizes everything into one schema, stores snapshots, and alerts on undercuts.

## Setup

```bash
pip install -r requirements.txt          # core needs only `requests`
cp .env.example .env                      # add your ScrapingBee key
export SCRAPINGBEE_API_KEY=...            # or use the .env file
```

Get a key (1,000 free credits, no card) at https://www.scrapingbee.com/.

## Use

1. Edit `targets.csv` — one row per competitor product:

   | column      | meaning                                                        |
   |-------------|----------------------------------------------------------------|
   | `our_sku`   | your internal SKU                                              |
   | `source`    | `amazon` (ASIN), `walmart` (item id), or `generic` (full URL)  |
   | `identifier`| the ASIN / item id / URL                                       |
   | `our_price` | your current price, for the undercut comparison               |

2. Run it:

   ```bash
   python tracker.py
   ```

   Output (real run):

   ```text
   MOUSE-001    amazon   USD 12.15 (ours 14.99)  <-- UNDERCUT by 18.9%
   MOUSE-002    walmart  USD 13.83 (ours 14.99)  <-- UNDERCUT by 7.7%
   HOODIE-009   generic  USD 69.0 (ours 75.0)  <-- UNDERCUT by 8.0%
   ```

   Snapshots append to `history.csv`. Set `SLACK_WEBHOOK_URL` to post alerts to Slack; otherwise they print.

3. Schedule it: commit and the included `.github/workflows/track.yml` runs it every 6 hours (set `SCRAPINGBEE_API_KEY` / `SLACK_WEBHOOK_URL` as repo secrets). Or use cron / a serverless cron.

## Files

| file | what it does |
|------|--------------|
| `tracker.py`    | fetch → normalize → store → alert (the main script) |
| `matcher.py`    | SKU matching: GTIN-first, fuzzy-title fallback with a confidence score |
| `mcp_server.py` | exposes the history as an MCP tool (`undercuts`) an assistant can call |
| `targets.csv`   | your watch list |

## Match before you trust the numbers

A competitor's "Logitech Wireless Mouse" may not be your SKU — comparing different products is a bug, not an insight. `matcher.py` matches on GTIN/UPC when both sides have one (ScrapingBee's Walmart parser returns `gtin`; Amazon's `product_details` often carries a UPC), then on **model code** — the real discriminator, since "M185" and "M510" have near-identical titles but are different products — and only then falls back to fuzzy title matching with a confidence score:

```bash
python matcher.py
#  [MATCH   ] conf=0.95 via model :: Logitech M185 Wireless Mouse, 2.4GHz with USB Mini Rece
#  [no match] conf=0.44 via title :: Logitech Silent Wireless Mouse, Blue/Gray, Walmart Excl
#  [no match] conf=0.1  via model :: Logitech M510 Wireless Mouse, 2.4 GHz USB Unifying Rece
```

Auto-accept high-confidence matches; queue `needs_review` pairs for a human.

## Honest limits (tested, June 2026)

- **AI extraction (`generic` rows) is flaky.** ScrapingBee sometimes returns HTTP 200 with `"Sorry, couldn't get the response from AI"` instead of JSON, and bills for it. `fetch_generic` retries up to 5× and raises if it never succeeds; the tracker skips that SKU rather than crashing.
- **Marketplace prices move and Walmart varies by store** — one item id returned $13.83 / $13.52 / $9.88 across calls. Pin a store with `store_id` for like-for-like comparisons.
- **Credit costs:** Amazon/Walmart parsers 5–15 each; HTML API 1 (no JS) / 5 (JS); +5 for AI extraction; stealth 75. Check `https://app.scrapingbee.com/api/v1/usage`.

## Credit cost per run

The sample `targets.csv` (1 Amazon + 1 Walmart + 1 generic-with-AI) costs ~25 credits per run.
