# Competitor Price Tracker

A small, working competitor-price tracker built on the [ScrapingBee](https://www.scrapingbee.com/) API. Companion to the article [*How to Track Competitor Prices Using Web Scraping*](https://www.scrapingbee.com/blog/how-to-track-competitor-pricing-with-scraping). Clone it, point it at your competitors, and get a Slack ping when one undercuts you.

It fetches Amazon and Walmart via ScrapingBee's dedicated parsers and most other retailers via the HTML API with AI extraction, normalizes everything into one schema, stores snapshots, and alerts on undercuts.

> **Status:** a point-in-time companion (last tested June 2026), not a maintained product. Dependencies, GitHub Actions versions, and the ScrapingBee API surface move over time — bump them as needed. The patterns are the durable part; the version pins and exact field names are not.

## Setup

Requires **Python 3.10+**.

```bash
pip install -r requirements.txt          # core: requests, beautifulsoup4, python-dotenv
cp .env.example .env                      # put your key in .env — it's auto-loaded
# ...or skip .env and: export SCRAPINGBEE_API_KEY=...
```

Get a key (free trial, no card) at https://www.scrapingbee.com/.

## Use

1. Edit `targets.csv` — one row per competitor product:

   | column      | meaning                                                        |
   |-------------|----------------------------------------------------------------|
   | `our_sku`   | your internal SKU                                              |
   | `source`    | `amazon` (ASIN), `walmart` (item id), or `generic` (full URL)  |
   | `identifier`| the ASIN / item id / URL                                       |
   | `our_price` | your current price, for the undercut comparison               |
   | `selector`  | *(optional, `generic` rows)* a CSS selector for the price, when auto-extraction can't find it |
   | `proxy`     | *(optional, `generic` rows)* `premium` or `stealth` for sites behind Cloudflare/DataDome (25 / 75 credits) |

2. Run it:

   ```bash
   python tracker.py
   ```

   Output (real run):

   ```text
   MOUSE-001    amazon   USD 13.99 (ours 14.99)  <-- UNDERCUT 6.7%
   MOUSE-002    walmart  USD 13.83 (ours 14.99)  <-- UNDERCUT 7.7%
   HOODIE-009   generic  USD 69.0 (ours 75.0)  <-- UNDERCUT 8.0% [OUT OF STOCK]
   ```

   (Prices are live, so yours will differ.) Snapshots append to `history.csv`. Set `SLACK_WEBHOOK_URL` to post alerts to Slack; otherwise they print. The out-of-stock row is flagged but **not** alerted.

3. Schedule it: the included `.github/workflows/track.yml` is **manual by default** (so a cloned repo never spends credits unattended). Uncomment its `schedule:` block to run every 6 hours, and add `SCRAPINGBEE_API_KEY` / `SLACK_WEBHOOK_URL` as repo secrets. It commits each snapshot back to the repo (git-scraping). Or use cron / a serverless cron.

## Files

| file | what it does |
|------|--------------|
| `tracker.py`       | fetch → normalize → store → alert (the main script) |
| `matcher.py`       | SKU matching: GTIN-first, then model code, then fuzzy title (with confidence) |
| `build_targets.py` | search a keyword + match results to your product → suggested `targets.csv` rows |
| `ai_surface.py`    | how an AI assistant prices/ranks a product right now (ScrapingBee ChatGPT endpoint) — the surface buyers increasingly check |
| `mcp_server.py`    | exposes the history as MCP tools (`undercuts` = current standings, `recent_changes` = who repriced) an assistant can call |
| `test_tracker.py`  | deterministic tests for the alert logic (no API) — `python test_tracker.py` |
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

## How it behaves

- **Generic rows use a deterministic cascade, AI last.** `fetch_generic` tries, in order: a **per-site CSS `selector`** you set in `targets.csv` → **JSON-LD** (`schema.org/Product`) → **OpenGraph/microdata price meta** → and only then **AI extraction**. The first three are deterministic and cost 1 credit; AI is the flaky/expensive fallback (it sometimes 200s with `"Sorry, couldn't get the response from AI"` instead of JSON, and still bills). So most sites resolve without AI — and for the awkward ones, **add a `selector` and the page usually becomes trackable** instead of failing. If all four miss, the tracker logs it and moves on rather than crashing.
- **Protected sites: escalate the proxy per target.** A site behind Cloudflare/DataDome blocks the cheap fetch, so the whole cascade comes up empty. Set `proxy=premium` (residential + JS, 25 credits) or `proxy=stealth` (hardest anti-bot, 75 credits) on that target and the cascade runs through the right proxy instead of getting blocked. It's per-target and opt-in, so you don't silently pay stealth rates for sites that don't need it.
- **Fetches run concurrently.** The tracker fans out with a thread pool (`MAX_WORKERS`, default 8 — set it to your plan's concurrency cap), so hundreds of SKUs refresh in the time a sequential loop handles a dozen. Results are still processed in order, so alerts and history stay deterministic.
- **Alerts fire on change, not every run.** You're pinged when a competitor *newly* undercuts you (or drops further) **and** is in stock — not every 6 hours for a competitor that's been cheaper all week. Change detection needs prior history, which is why CI commits `history.csv` back (below).
- **Only comparable offers alert.** A marketplace listing's headline price is whatever wins the Buy Box — which can be a *used* unit, a *third-party* seller, or a *multipack* variant. `fetch_amazon` reads the Buy Box condition + seller, records both in every snapshot (so a wrong-offer comparison is auditable, not invisible), and a used or multipack offer is logged with a `⚠` note but **does not fire an undercut alert** — a used unit at $7.72 isn't an undercut of your new $14.99.
- **History accumulates via git-scraping.** `history.csv` is tracked on purpose: the GitHub Actions job checks it out, appends the new run, and commits it back, so price history builds up in git (diffable over time). Running locally also appends to it — that's expected. Trade-offs: it adds a commit (and grows the CSV) every run, so over a year of 6-hourly runs the repo carries ~1,500 small commits (squash or roll to a database if that bothers you); and the bot pushes to the default branch, so if you **protect** that branch, point the workflow at an unprotected data branch or a database instead.
- **Cross-currency is not compared — to the limit of what a price string reveals.** Prices are compared only within `OUR_CURRENCY` (default USD); there's **no FX conversion**, so a competitor in another currency is recorded and flagged `[currency …≠USD, not compared]` instead of mis-compared (€63 is not "cheaper" than $75). The honest caveat: this leans on the currency *code*. `€`/`£` are unambiguous, but `$` is shared by USD/CAD/AUD/MXN/… and can't be told apart from a bare symbol — so a symbol-only `$` is **assumed to be your `OUR_CURRENCY`** (`_symbol_currency` keeps a clearly-foreign symbol like `¥` foreign, but two dollar currencies are indistinguishable). The dedicated parsers and JSON-LD return an explicit code, so `CAD`≠`USD` *is* caught there; the gap is only the generic symbol-only path. Track same-market competitors there, or use sources that expose a currency code. Add FX if you compare across currencies.
- **Marketplace prices move; Walmart varies by store.** One item id returned $13.83 / $13.52 / $9.88 across calls. Walmart's `store_id` is *meant* to pin a store for like-for-like comparison, but in testing it was slow/intermittent — verify it before relying on it.
- **US-centric by design.** The Walmart parser is US-only and the worked example is Amazon.com/USD. Outside the US, use Amazon's `domain` and the generic path; Walmart won't apply.

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
- `pip install` fails on a very new/locked-down Python → install the core only (`pip install requests beautifulsoup4 python-dotenv`); `duckdb`/`fastmcp`/`pandas` are needed only for `mcp_server.py`.

## Tests

```bash
python test_tracker.py
```

Covers the parts that are easy to get wrong — alert-on-change (no every-run spam), in-stock gating, cross-currency safety, history accumulation — with mocked fetchers, so it runs offline and costs no credits.

## License

MIT — see [LICENSE](LICENSE). Use it, fork it, ship it.
