"""
News MCP Server
-----------------
Exposes recent news headlines for a stock symbol via Google News RSS.
Free, no API key required.
"""

import feedparser
from mcp.server import MCPServer

mcp = MCPServer("news")


@mcp.tool()
def get_recent_news(symbol: str, max_headlines: int = 5) -> dict:
    """
    Get recent news headlines related to a stock symbol.

    Args:
        symbol: Stock ticker or company name, e.g. "AAPL" or "Apple".
        max_headlines: Maximum number of headlines to return.
    """
    try:
        query = symbol.replace(" ", "+")
        url = f"https://news.google.com/rss/search?q={query}+stock&hl=en-US&gl=US&ceid=US:en"
        feed = feedparser.parse(url)

        if not feed.entries:
            return {"symbol": symbol, "headlines": [], "note": "No recent news found."}

        headlines = [
            {
                "title": entry.get("title", "Untitled"),
                "source": entry.get("source", {}).get("title", "Unknown"),
                "published": entry.get("published", "Unknown"),
            }
            for entry in feed.entries[:max_headlines]
        ]
        return {"symbol": symbol, "headlines": headlines}
    except Exception as e:
        return {"symbol": symbol, "error": f"Failed to fetch news: {e}"}


if __name__ == "__main__":
    mcp.run(transport="stdio")
