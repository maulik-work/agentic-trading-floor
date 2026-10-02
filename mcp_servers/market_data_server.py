"""
Market Data MCP Server
-----------------------
Wraps yfinance so the Research Agent (and Risk Agent, for price-only checks)
can look up live prices, history, company fundamentals, technical
indicators, and a NIFTY-50 market comparison.

yfinance is an *unofficial* scraper of Yahoo Finance, not a real API, and
Yahoo rate-limits IPs that call it too often. Streamlit Community Cloud
runs many free apps on shared IP ranges, so once that range gets flagged,
every symbol fails identically - which is exactly the bug this file fixes.

Two defenses, both wrapped around every tool below:
  1. File-based caching - most repeat lookups (same symbol, same minute)
     never hit Yahoo at all.
  2. Retry-with-backoff, specific to yfinance's own YFRateLimitError - a
     transient 429 gets a couple of retries; anything else (bad symbol,
     network error) fails fast instead of being retried pointlessly.
  3. If retries are exhausted, a stale cached copy (if one exists) is
     served rather than failing outright, so the user still gets an answer.
"""

import hashlib
import json
import time
from pathlib import Path

import pandas as pd
import yfinance as yf
from mcp.server import MCPServer
from yfinance.exceptions import YFRateLimitError

mcp = MCPServer("market-data")

# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# How long a cached result stays "fresh" per tool, in seconds. Prices move
# fast; fundamentals barely change minute to minute.
CACHE_TTL = {
    "get_current_price": 120,
    "get_price_history": 300,
    # Fundamentals change rarely (a company's sector doesn't change
    # intraday), and ticker.info hits a heavier, separately-rate-limited
    # Yahoo endpoint than price/history calls - so it's cached much longer
    # to keep us off it as much as possible.
    "get_company_info": 21600,  # 6 hours
    "get_technical_indicators": 300,
    "get_market_comparison": 300,
}


def _cache_path(tool_name: str, cache_key: str) -> Path:
    digest = hashlib.sha1(cache_key.encode()).hexdigest()[:16]
    return CACHE_DIR / f"{tool_name}_{digest}.json"


def _read_cache(tool_name: str, cache_key: str, allow_stale: bool = False):
    path = _cache_path(tool_name, cache_key)
    if not path.exists():
        return None
    try:
        with open(path, "r") as f:
            payload = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None

    age = time.time() - payload.get("cached_at", 0)
    ttl = CACHE_TTL.get(tool_name, 300)
    if allow_stale or age <= ttl:
        return payload.get("data")
    return None


def _write_cache(tool_name: str, cache_key: str, data) -> None:
    path = _cache_path(tool_name, cache_key)
    try:
        with open(path, "w") as f:
            json.dump({"cached_at": time.time(), "data": data}, f)
    except OSError:
        pass  # caching is best-effort - never let it break a real call


def _with_cache_and_retry(tool_name: str, cache_key: str, fetch_fn, max_attempts: int = 3):
    """
    Shared pipeline every tool below runs through:
      fresh cache hit -> return it (no network call at all)
      else -> call fetch_fn(), retrying ONLY on YFRateLimitError
      success -> cache it and return it
      all retries exhausted -> fall back to a stale cache if one exists
      no stale cache either -> re-raise so the caller's except returns
                                a clean {"error": ...} dict
    """
    cached = _read_cache(tool_name, cache_key)
    if cached is not None:
        return cached

    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            data = fetch_fn()
            _write_cache(tool_name, cache_key, data)
            return data
        except YFRateLimitError as e:
            last_error = e
            if attempt < max_attempts:
                time.sleep(2 ** attempt)  # 2s, 4s
            continue

    stale = _read_cache(tool_name, cache_key, allow_stale=True)
    if stale is not None:
        stale = dict(stale, stale=True)  # flag it so callers/agents know
        return stale

    raise last_error


def safe(value):
    """Cast yfinance/numpy scalar types to plain Python types for JSON."""
    if value is None:
        return None
    if isinstance(value, (int, float, str, bool)):
        return value
    try:
        return value.item()
    except AttributeError:
        return str(value)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
def get_current_price(symbol: str) -> dict:
    """
    Get the current/latest market price for a stock.

    Args:
        symbol: Stock ticker, e.g. "AAPL" or "RELIANCE.NS".
    """
    try:
        def fetch():
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="5d")
            if hist.empty:
                raise ValueError(f"No price data found for {symbol}")
            last_close = float(hist["Close"].iloc[-1])
            prev_close = float(hist["Close"].iloc[-2]) if len(hist) > 1 else last_close
            change_pct = ((last_close - prev_close) / prev_close) * 100 if prev_close else 0.0
            return {
                "symbol": symbol,
                "price": round(last_close, 2),
                "previous_close": round(prev_close, 2),
                "change_pct": round(change_pct, 2),
            }

        return _with_cache_and_retry("get_current_price", symbol, fetch)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_price_history(symbol: str, period: str = "1mo") -> dict:
    """
    Get historical daily prices for a stock.

    Args:
        symbol: Stock ticker, e.g. "AAPL" or "RELIANCE.NS".
        period: History window, e.g. "5d", "1mo", "3mo", "6mo", "1y".
    """
    try:
        def fetch():
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=period)
            if hist.empty:
                raise ValueError(f"No price history found for {symbol}")
            records = [
                {
                    "date": str(idx.date()),
                    "close": round(float(row["Close"]), 2),
                    "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
                }
                for idx, row in hist.iterrows()
            ]
            return {
                "symbol": symbol,
                "period": period,
                "52w_high": round(float(hist["High"].max()), 2),
                "52w_low": round(float(hist["Low"].min()), 2),
                "history": records,
            }

        return _with_cache_and_retry("get_price_history", f"{symbol}:{period}", fetch)
    except Exception as e:
        return {"error": str(e)}


def _fast_info_fallback(symbol: str) -> dict:
    """
    ticker.info hits Yahoo's heavy quoteSummary endpoint and gets
    rate-limited independently of (and more often than) price/history
    calls. ticker.fast_info hits a lighter endpoint and survives when
    .info doesn't - it has no sector/industry/name, but still gives
    market cap, currency and P/E-adjacent figures, which is better than
    nothing for the dashboard.
    """
    fast = yf.Ticker(symbol).fast_info
    return {
        "symbol": symbol,
        "name": None,
        "sector": None,
        "industry": None,
        "market_cap": safe(fast.get("marketCap") or fast.get("market_cap")),
        "pe_ratio": None,
        "currency": safe(fast.get("currency")),
        "partial": True,  # tells the dashboard this came from the fallback
    }


@mcp.tool()
def get_company_info(symbol: str) -> dict:
    """
    Get company fundamentals: sector, market cap, P/E ratio, etc.

    Args:
        symbol: Stock ticker, e.g. "AAPL" or "RELIANCE.NS".
    """
    try:
        def fetch():
            ticker = yf.Ticker(symbol)
            info = ticker.info
            return {
                "symbol": symbol,
                "name": safe(info.get("longName") or info.get("shortName")),
                "sector": safe(info.get("sector")),
                "industry": safe(info.get("industry")),
                "market_cap": safe(info.get("marketCap")),
                "pe_ratio": safe(info.get("trailingPE")),
                "currency": safe(info.get("currency")),
            }

        return _with_cache_and_retry("get_company_info", symbol, fetch)
    except YFRateLimitError:
        # .info is fully exhausted (retries + no stale cache available).
        # Try the lighter fast_info endpoint before giving up entirely.
        try:
            fallback = _fast_info_fallback(symbol)
            _write_cache("get_company_info", symbol, fallback)
            return fallback
        except Exception as e:
            return {"error": f"Company info unavailable (rate-limited): {e}"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_technical_indicators(symbol: str) -> dict:
    """
    Get technical indicators for a stock: 14-day RSI, 50/200-day moving
    averages, golden/death cross signal, and recent volume trend.

    Args:
        symbol: Stock ticker, e.g. "AAPL" or "RELIANCE.NS".
    """
    try:
        def fetch():
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1y")
            if hist.empty or len(hist) < 20:
                raise ValueError(f"Not enough price history for {symbol} to compute indicators")

            close = hist["Close"]

            delta = close.diff()
            gain = delta.clip(lower=0)
            loss = -delta.clip(upper=0)
            avg_gain = gain.rolling(window=14).mean()
            avg_loss = loss.rolling(window=14).mean()
            rs = avg_gain / avg_loss.replace(0, pd.NA)
            rsi_series = 100 - (100 / (1 + rs))
            rsi = float(rsi_series.dropna().iloc[-1]) if not rsi_series.dropna().empty else None

            ma50 = float(close.rolling(window=50).mean().iloc[-1]) if len(close) >= 50 else None
            ma200 = float(close.rolling(window=200).mean().iloc[-1]) if len(close) >= 200 else None

            cross = None
            if ma50 is not None and ma200 is not None:
                cross = "golden_cross" if ma50 > ma200 else "death_cross"

            recent_vol = hist["Volume"].tail(5).mean()
            older_vol = hist["Volume"].iloc[:-5].tail(20).mean() if len(hist) > 25 else recent_vol
            volume_trend = "increasing" if recent_vol > older_vol else "decreasing"

            return {
                "symbol": symbol,
                "rsi_14": round(rsi, 2) if rsi is not None else None,
                "ma_50": round(ma50, 2) if ma50 is not None else None,
                "ma_200": round(ma200, 2) if ma200 is not None else None,
                "cross_signal": cross,
                "volume_trend": volume_trend,
            }

        return _with_cache_and_retry("get_technical_indicators", symbol, fetch)
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def get_market_comparison(symbol: str) -> dict:
    """
    Compare a stock's recent performance against the NIFTY 50 index.

    Args:
        symbol: Stock ticker, e.g. "RELIANCE.NS".
    """
    try:
        def fetch():
            stock_hist = yf.Ticker(symbol).history(period="1mo")
            index_hist = yf.Ticker("^NSEI").history(period="1mo")
            if stock_hist.empty or index_hist.empty:
                raise ValueError(f"Not enough data to compare {symbol} against NIFTY 50")

            stock_return = (
                (stock_hist["Close"].iloc[-1] - stock_hist["Close"].iloc[0])
                / stock_hist["Close"].iloc[0]
            ) * 100
            index_return = (
                (index_hist["Close"].iloc[-1] - index_hist["Close"].iloc[0])
                / index_hist["Close"].iloc[0]
            ) * 100

            return {
                "symbol": symbol,
                "stock_1mo_return_pct": round(float(stock_return), 2),
                "nifty50_1mo_return_pct": round(float(index_return), 2),
                "outperforming_market": bool(stock_return > index_return),
            }

        return _with_cache_and_retry("get_market_comparison", symbol, fetch)
    except Exception as e:
        return {"error": str(e)}


if __name__ == "__main__":
    mcp.run()
  
