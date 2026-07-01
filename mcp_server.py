"""Expose the price history as MCP tools an assistant can call.

Run it, point your MCP client (Claude Desktop, etc.) at it, and ask in plain language:
"who's undercutting us right now?" or "who repriced this week?"

  pip install fastmcp duckdb pandas
  python mcp_server.py
"""
import os

import duckdb
from fastmcp import FastMCP

mcp = FastMCP("price-tracker")
HISTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.csv")


def _guard() -> list[dict] | None:
    """history.csv missing or header-only — DuckDB can't sniff a data-less CSV."""
    if not os.path.exists(HISTORY):
        return [{"error": "history.csv not found — run tracker.py first"}]
    with open(HISTORY, encoding="utf-8") as f:
        if sum(1 for _ in f) <= 1:
            return []
    return None


@mcp.tool()
def undercuts(min_pct: float = 5.0) -> list[dict]:
    """SKUs whose competitor's LATEST snapshot undercuts our price by at least min_pct percent.

    Reduces to one row per SKU (the most recent observation) before comparing — so a SKU
    that's been undercut across 100 runs returns once, not 100 times.
    """
    if (g := _guard()) is not None:
        return g
    # CAST so the math works whether DuckDB infers the columns as numbers or text.
    return duckdb.execute(
        "SELECT our_sku, competitor, comp_price, our_price, "
        "       round((our_price - comp_price) / our_price * 100, 1) AS undercut_pct "
        "FROM (SELECT our_sku, competitor, CAST(comp_price AS DOUBLE) AS comp_price, "
        "             CAST(our_price AS DOUBLE) AS our_price, "
        "             ROW_NUMBER() OVER (PARTITION BY our_sku ORDER BY timestamp DESC) AS rn "
        f"      FROM '{HISTORY}') "
        "WHERE rn = 1 AND comp_price < our_price * (1 - ? / 100) "
        "ORDER BY undercut_pct DESC",
        [min_pct],
    ).df().to_dict("records")


@mcp.tool()
def recent_changes(days: float = 7.0) -> list[dict]:
    """SKUs whose competitor price MOVED since the previous snapshot, within the last `days`.

    Answers "who repriced this week?" — the question only the price *history* can answer,
    not a single fetch. Each row shows the prior price, the latest price, and the % move.
    """
    if (g := _guard()) is not None:
        return g
    return duckdb.execute(
        "SELECT our_sku, competitor, prev_price, comp_price AS latest_price, "
        "       round((comp_price - prev_price) / prev_price * 100, 1) AS pct_change, "
        "       timestamp AS changed_at "
        "FROM (SELECT our_sku, competitor, timestamp, "
        "             CAST(comp_price AS DOUBLE) AS comp_price, "
        "             LAG(CAST(comp_price AS DOUBLE)) OVER (PARTITION BY our_sku ORDER BY timestamp) AS prev_price, "
        "             ROW_NUMBER() OVER (PARTITION BY our_sku ORDER BY timestamp DESC) AS rn "
        f"      FROM '{HISTORY}') "
        "WHERE rn = 1 AND prev_price IS NOT NULL AND comp_price <> prev_price "
        "  AND CAST(timestamp AS TIMESTAMP) >= now() - ? * INTERVAL 1 DAY "
        "ORDER BY abs(pct_change) DESC",
        [days],
    ).df().to_dict("records")


if __name__ == "__main__":
    mcp.run()
