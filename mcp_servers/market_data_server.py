"""
Market Data MCP Server
-----------------------
Exposes live/recent stock price data as MCP tools, backed by yfinance.
"""

from mcp.server import MCPServer
import yfinance as yf

mcp = MCPServer("market-data")


@mcp.tool()
def get_current_price(symbol: str) -> dict:
    """
    Get the most recent price for a stock symbol.

    Args:
        symbol: Stock ticker, e.g. "AAPL", "RELIANCE.NS" for NSE stocks.
    """
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1d", interval="1m")
    except Exception as e:
        return {"symbol": symbol, "error": f"Failed to fetch data: {e}"}

    if data.empty:
        return {"symbol": symbol, "error": "No data returned. Check the symbol."}

    last_row = data.iloc[-1]
    volume = last_row["Volume"]
    return {
        "symbol": symbol,
        "price": round(float(last_row["Close"]), 2),
        "volume": int(volume) if volume == volume else 0,  # NaN check
        "timestamp": str(data.index[-1]),
    }


@mcp.tool()
def get_price_history(symbol: str, period: str = "5d", interval: str = "1d") -> dict:
    """
    Get historical price data for a stock, useful for trend analysis.

    Args:
        symbol: Stock ticker, e.g. "AAPL".
        period: How far back, e.g. "5d", "1mo", "3mo".
        interval: Bar size, e.g. "1d", "1h".
    """
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period=period, interval=interval)
    except Exception as e:
        return {"symbol": symbol, "error": f"Failed to fetch data: {e}"}

    if data.empty:
        return {"symbol": symbol, "error": "No data returned. Check the symbol/period."}

    candles = [
        {
            "date": str(idx),
            "open": round(float(row["Open"]), 2),
            "high": round(float(row["High"]), 2),
            "low": round(float(row["Low"]), 2),
            "close": round(float(row["Close"]), 2),
            "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
        }
        for idx, row in data.iterrows()
    ]
    return {"symbol": symbol, "candles": candles}


@mcp.tool()
def get_company_info(symbol: str) -> dict:
    """
    Get basic company fundamentals for a stock symbol.

    Args:
        symbol: Stock ticker, e.g. "AAPL".
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
    except Exception as e:
        return {"symbol": symbol, "error": f"Failed to fetch data: {e}"}

    def safe(key, cast=None):
        val = info.get(key)
        if val is None:
            return "N/A"
        if cast:
            try:
                return cast(val)
            except (TypeError, ValueError):
                return "N/A"
        return val

    return {
        "symbol": symbol,
        "name": safe("longName", str),
        "sector": safe("sector", str),
        "market_cap": safe("marketCap", int),
        "pe_ratio": safe("trailingPE", float),
        "52_week_high": safe("fiftyTwoWeekHigh", float),
        "52_week_low": safe("fiftyTwoWeekLow", float),
    }


@mcp.tool()
def get_moving_averages(symbol: str) -> dict:
    """
    Get 50-day and 200-day moving averages for a stock, using 1 year of
    price history. This gives a real trend read, unlike a 5-day window -
    whether the price is above/below its longer-term averages, and
    whether the shorter average is above the longer one (a "golden cross"
    pattern, generally read as bullish) or below it ("death cross",
    generally read as bearish).

    Args:
        symbol: Stock ticker, e.g. "AAPL".
    """
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="1y", interval="1d")
    except Exception as e:
        return {"symbol": symbol, "error": f"Failed to fetch data: {e}"}

    if data.empty or len(data) < 50:
        return {"symbol": symbol, "error": "Not enough historical data (need at least 50 trading days)."}

    close = data["Close"]
    current_price = float(close.iloc[-1])
    sma_50 = float(close.rolling(window=50).mean().iloc[-1])
    sma_200 = float(close.rolling(window=200).mean().iloc[-1]) if len(close) >= 200 else None

    result = {
        "symbol": symbol,
        "current_price": round(current_price, 2),
        "sma_50": round(sma_50, 2),
        "price_vs_sma_50": "above" if current_price > sma_50 else "below",
    }

    if sma_200 is not None:
        result["sma_200"] = round(sma_200, 2)
        result["price_vs_sma_200"] = "above" if current_price > sma_200 else "below"
        result["sma_50_vs_sma_200"] = (
            "golden_cross (50-day above 200-day, generally bullish)"
            if sma_50 > sma_200
            else "death_cross (50-day below 200-day, generally bearish)"
        )
    else:
        result["note"] = "Less than 200 days of history available - 200-day average not calculated."

    return result


@mcp.tool()
def get_fundamentals(symbol: str) -> dict:
    """
    Get multi-year company health indicators: revenue growth, earnings
    growth, debt-to-equity ratio, and return on equity. This is real
    fundamental analysis, unlike just checking P/E ratio and market cap
    in isolation.

    Args:
        symbol: Stock ticker, e.g. "AAPL".
    """
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
    except Exception as e:
        return {"symbol": symbol, "error": f"Failed to fetch data: {e}"}

    def safe(key, cast=None):
        val = info.get(key)
        if val is None:
            return "N/A"
        if cast:
            try:
                return cast(val)
            except (TypeError, ValueError):
                return "N/A"
        return val

    result = {
        "symbol": symbol,
        "revenue_growth_yoy": safe("revenueGrowth", float),
        "earnings_growth_yoy": safe("earningsGrowth", float),
        "debt_to_equity": safe("debtToEquity", float),
        "return_on_equity": safe("returnOnEquity", float),
        "total_debt": safe("totalDebt", int),
        "total_cash": safe("totalCash", int),
    }

    try:
        financials = ticker.financials
        if not financials.empty and "Total Revenue" in financials.index:
            revenue_by_year = financials.loc["Total Revenue"].dropna()
            result["revenue_last_n_years"] = {
                str(date.year): round(float(val), 0) for date, val in revenue_by_year.items()
            }
    except Exception:
        result["revenue_last_n_years"] = "N/A (could not fetch multi-year financials)"

    return result


if __name__ == "__main__":
    mcp.run(transport="stdio")
