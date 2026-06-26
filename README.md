# Competitor Price Tracker

A small, working competitor-price tracker built on the [ScrapingBee](https://www.scrapingbee.com/) API. Companion to the article *How to Track Competitor Prices Using Web Scraping*. Clone it, point it at your competitors, and get a Slack ping when one undercuts you.

It fetches Amazon and Walmart via ScrapingBee's dedicated parsers and any other retailer via the HTML API with AI extraction, normalizes everything into one schema, stores snapshots, and alerts on undercuts.

## Setup

Requires **Python 3.10+**.

```bash
pip install -r requirements.txt          # core: requests, beautifulsoup4, python-dotenv
cp .env.example .env                      # put your key in .env — it's auto-loaded
# ...or skip .env and: export SCRAPINGBEE_API_KEY=...
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
   MOUSE-001    amazon   USD 12.15 (ours 14.99)  <-- UNDERCUT 18.9%
   MOUSE-002    walmart  USD 13.83 (ours 14.99)  <-- UNDERCUT 7.7%
   HOODIE-009   generic  USD 69.0 (ours 75.0)  <-- UNDERCUT 8.0% [OUT OF STOCK]
   ```

   (Prices are live, so yours will differ.) Snapshots append to `history.csv`. Set `SLACK_WEBHOOK_URL` to post alerts to Slack; otherwise they print. The out-of-stock row is flagged but **not** alerted.

3. Schedule it: commit and the included `.github/workflows/track.yml` runs it every 6 hours (set `SCRAPINGBEE_API_KEY` / `SLACK_WEBHOOK_URL` as repo secrets). Or use cron / a serverless cron.

## Files

| file | what it does |
|------|--------------|
| `tracker.py`       | fetch → normalize → store → alert (the main script) |
| `matcher.py`       | SKU matching: GTIN-first, then model code, then fuzzy title (with confidence) |
| `build_targets.py` | search a keyword + match results to your product → suggested `targets.csv` rows |
| `mcp_server.py`    | exposes the history as an MCP tool (`undercuts`) an assistant can call |
| `targets.csv`      | your watch list |

## Match before you trust the numbers

A competitor's "Logitech Wireless Mouse" may not be your SKU — comparing different products is a bug, not an insight. `matcher.py` matches on GTIN/UPC when both sides have one (ScrapingBee's Walmart parser returns `gtin`; Amazon's `product_details` often carries a UPC), then on **model code** — the real discriminator, since "M185" and "M510" have near-identical titles but are different products — and only then falls back to fuzzy title matching with a confidence score:

```bash
python matcher.py
#  [MATCH   ] conf=0.95 via model :: Logitech M185 Wireless Mouse, 2.4GHz with USB Mini Rece
#  [no match] conf=0.44 via title :: Logitech Silent Wireless Mouse, Blue/Gray, Walmart Excl
#  [no match] conf=0.1  via model :: Logitech M510 Wireless Mouse, 2.4 GHz USB Unifying Rece
```

Auto-accept high-confidence matches; queue `needs_review` pairs for a human. **This matcher is a starting point, not a finished system** — it handles the demo cleanly, but real catalogs (thousands of SKUs, missing GTINs, variants, bundles, refurb-vs-new) need more than a regex and `difflib`. Treat it as the skeleton to build on, and lean on GTIN/UPC whenever your data has it.

Matching is a *setup-time* step, so it isn't in the tracking loop — you decide what to track once. `build_targets.py` does that step for you: it searches a keyword, matches each result to your product, and prints the `targets.csv` rows worth keeping.

```bash
python build_targets.py "Logitech M185 Wireless Mouse" "logitech wireless mouse" 14.99
# LOGITECH-B004YA,amazon,B004YAVF8I,14.99    # conf=0.95 via model [ok] M185 Wireless Mouse, 2.4GHz ...
```

## How it behaves (so nothing surprises you)

- **Generic rows are JSON-LD-first.** `fetch_generic` parses a `schema.org/Product` block first (deterministic, 1 credit, no JS) and only falls back to AI extraction if the page has none. That dodges the biggest reliability problem: AI extraction sometimes returns HTTP 200 with `"Sorry, couldn't get the response from AI"` instead of JSON — and still bills. Even so, a site with neither JSON-LD nor AI-extractable content will fail; the tracker logs it and moves on rather than crashing.
- **Alerts fire on change, not every run.** You're pinged when a competitor *newly* undercuts you (or drops further) **and** is in stock — not every 6 hours for a competitor that's been cheaper all week. Change detection needs prior history, which is why CI commits `history.csv` back (below).
- **History accumulates via git-scraping.** `history.csv` is tracked on purpose: the GitHub Actions job checks it out, appends the new run, and commits it back, so price history builds up in git (diffable over time). Running locally also appends to it — that's expected. Trade-off: this adds a commit (and grows the CSV) every run, so over a year of 6-hourly runs the repo carries ~1,500 small commits — squash or roll the history to a database if that bothers you.
- **Marketplace prices move; Walmart varies by store.** One item id returned $13.83 / $13.52 / $9.88 across calls. Walmart's `store_id` is *meant* to pin a store for like-for-like comparison, but in testing it was slow/intermittent — verify it before relying on it.

## Cost — do the math before you scale

| scope | credits/run | rough monthly (daily run) |
|---|---|---|
| 3 SKUs (this demo) | ~20–25 | negligible |
| 100 SKUs | ~500–1,500 | ~15k–45k |
| 500 SKUs | ~2,500–7,500 | ~75k–225k |

(Amazon/Walmart parsers 5–15 each; HTML API 1 no-JS / 5 JS; +5 for AI extraction.) The free trial's 1,000 credits is enough to *evaluate*, not to run a real catalog — budget a paid plan for production, and check spend at `https://app.scrapingbee.com/api/v1/usage`.

## Troubleshooting

- `401` / "check SCRAPINGBEE_API_KEY" → key missing or wrong; the tracker exits with that message.
- `429` → you exceeded your plan's concurrency cap; the session already retries with backoff.
- `pip install` fails on a very new/locked-down Python → install the core only (`pip install requests beautifulsoup4`); `duckdb`/`mcp` are needed only for `mcp_server.py`.
