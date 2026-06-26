"""Expose the price history as an MCP tool an assistant can call.

Run it, point your MCP client (Claude Desktop, etc.) at it, and ask
"who's undercutting us this week?" in plain language.

  pip install mcp duckdb
  python mcp_server.py
"""
import duckdb
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("price-tracker")


@mcp.tool()
def undercuts(min_pct: float = 5.0) -> list[dict]:
    """SKUs where a competitor undercuts our price by at least min_pct percent."""
    return duckdb.execute(
        "SELECT our_sku, competitor, comp_price, our_price "
        "FROM 'history.csv' WHERE comp_price < our_price * (1 - ? / 100)",
        [min_pct],
    ).df().to_dict("records")


if __name__ == "__main__":
    mcp.run()
