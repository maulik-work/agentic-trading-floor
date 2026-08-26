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


@mcp.tool()
def get_technical_indicators(symbol: str) -> dict:
    """
    Get real technical indicators: 14-day RSI (Relative Strength Index)
    and volume trend (recent 5-day average volume vs 3-month average).
    RSI above 70 is generally considered overbought (may be due for a
    pullback), below 30 oversold (may be due for a bounce). This is
    genuine momentum analysis, distinct from moving averages (trend)
    or fundamentals (business health).

    Args:
        symbol: Stock ticker, e.g. "AAPL".
    """
    try:
        ticker = yf.Ticker(symbol)
        data = ticker.history(period="3mo", interval="1d")
    except Exception as e:
        return {"symbol": symbol, "error": f"Failed to fetch data: {e}"}

    if data.empty or len(data) < 15:
        return {"symbol": symbol, "error": "Not enough historical data for RSI (need at least 15 trading days)."}

    close = data["Close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=14).mean()
    avg_loss = loss.rolling(window=14).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))

    latest_rsi = float(rsi.iloc[-1]) if rsi.iloc[-1] == rsi.iloc[-1] else None  # NaN check

    volume = data["Volume"]
    recent_avg_volume = float(volume.tail(5).mean())
    baseline_avg_volume = float(volume.mean())
    volume_ratio = recent_avg_volume / baseline_avg_volume if baseline_avg_volume > 0 else None

    result = {"symbol": symbol}

    if latest_rsi is not None:
        result["rsi_14"] = round(latest_rsi, 1)
        if latest_rsi >= 70:
            result["rsi_signal"] = "overbought (RSI >= 70)"
        elif latest_rsi <= 30:
            result["rsi_signal"] = "oversold (RSI <= 30)"
        else:
            result["rsi_signal"] = "neutral"
    else:
        result["rsi_14"] = "N/A"

    if volume_ratio is not None:
        result["recent_avg_volume"] = int(recent_avg_volume)
        result["volume_vs_3mo_avg"] = f"{round(volume_ratio, 2)}x"

    return result


@mcp.tool()
def get_market_comparison(symbol: str, period: str = "1mo") -> dict:
    """
    Compare this stock's return over the period against the NIFTY 50
    index (the broad Indian market benchmark) - whether it's actually
    out- or under-performing the overall market, not just moving up or
    down in isolation. A stock can rise 5% and still be a laggard if
    the whole market rose 10%.

    Args:
        symbol: Stock ticker, e.g. "RELIANCE.NS".
        period: Comparison window, e.g. "1mo", "3mo".
    """
    try:
        stock_data = yf.Ticker(symbol).history(period=period, interval="1d")
        index_data = yf.Ticker("^NSEI").history(period=period, interval="1d")
    except Exception as e:
        return {"symbol": symbol, "error": f"Failed to fetch data: {e}"}

    if stock_data.empty or index_data.empty or len(stock_data) < 2 or len(index_data) < 2:
        return {"symbol": symbol, "error": "Not enough data for comparison."}

    stock_return = (float(stock_data["Close"].iloc[-1]) / float(stock_data["Close"].iloc[0]) - 1) * 100
    index_return = (float(index_data["Close"].iloc[-1]) / float(index_data["Close"].iloc[0]) - 1) * 100
    relative_performance = stock_return - index_return

    return {
        "symbol": symbol,
        "period": period,
        "stock_return_pct": round(stock_return, 2),
        "nifty50_return_pct": round(index_return, 2),
        "relative_performance_pct": round(relative_performance, 2),
        "outperforming_market": relative_performance > 0,
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
