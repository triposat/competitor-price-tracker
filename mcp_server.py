"""Expose the price history as an MCP tool an assistant can call.

Run it, point your MCP client (Claude Desktop, etc.) at it, and ask
"who's undercutting us this week?" in plain language.

  pip install fastmcp duckdb
  python mcp_server.py
"""
import os

import duckdb
from fastmcp import FastMCP

mcp = FastMCP("price-tracker")
HISTORY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.csv")


@mcp.tool()
def undercuts(min_pct: float = 5.0) -> list[dict]:
    """SKUs where a competitor undercuts our price by at least min_pct percent."""
    if not os.path.exists(HISTORY):
        return [{"error": "history.csv not found — run tracker.py first"}]
    with open(HISTORY, encoding="utf-8") as f:
        if sum(1 for _ in f) <= 1:        # header only / empty — DuckDB can't sniff a data-less CSV
            return []
    # CAST so the comparison works whether DuckDB infers the columns as numbers or text
    return duckdb.execute(
        "SELECT our_sku, competitor, "
        "CAST(comp_price AS DOUBLE) AS comp_price, CAST(our_price AS DOUBLE) AS our_price "
        f"FROM '{HISTORY}' "
        "WHERE CAST(comp_price AS DOUBLE) < CAST(our_price AS DOUBLE) * (1 - ? / 100)",
        [min_pct],
    ).df().to_dict("records")


if __name__ == "__main__":
    mcp.run()
