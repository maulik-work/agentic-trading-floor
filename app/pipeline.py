"""
Agentic Trading Floor - Core Pipeline
----------------------------------------
Shared pipeline logic used by BOTH the CLI (main.py) and the dashboard
(dashboard.py). Separating this from presentation means the same tested
logic drives both interfaces - no duplicated agent-wiring code.

run_pipeline_stream() is an async generator: it yields a small event dict
after each stage starts/finishes, so a caller can show live progress
instead of waiting silently for the whole pipeline to finish. run_pipeline()
is a thin wrapper for callers (like the CLI) that just want the final result.
"""

import sys
import os
import json
import re
import asyncio

PYTHON_EXECUTABLE = sys.executable
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(REPO_ROOT)

import openai
from agents import Runner
from agents.mcp import MCPServerStdio, create_static_tool_filter

from trading_agents.research_agent import build_research_agent
from trading_agents.risk_agent import build_risk_agent
from trading_agents.trader_agent import build_trader_agent

MARKET_DATA_SERVER_PATH = os.path.join(REPO_ROOT, "mcp_servers", "market_data_server.py")
PORTFOLIO_SERVER_PATH = os.path.join(REPO_ROOT, "mcp_servers", "portfolio_server.py")
NEWS_SERVER_PATH = os.path.join(REPO_ROOT, "mcp_servers", "news_server.py")
PORTFOLIOS_DIR = os.path.join(REPO_ROOT, "data", "portfolios")

DEFAULT_TRADE_QTY = 10
TIMEOUT_SECONDS = 30
DEFAULT_PROFILE_ID = "default"


def sanitize_profile_id(raw: str) -> str:
    """Sanitize a profile id to a safe filename component (alnum, dash, underscore only)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", raw or "").strip()
    return cleaned or DEFAULT_PROFILE_ID


def normalize_nse_symbol(symbol: str) -> str:
    """Append .NS if the user typed a bare NSE symbol, e.g. 'OLAELEC' -> 'OLAELEC.NS'."""
    symbol = symbol.strip().upper()
    if "." not in symbol:
        symbol = f"{symbol}.NS"
    return symbol


_SYMBOL_SHAPE_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-&]{0,19}$")


def looks_like_a_symbol(normalized_symbol: str) -> bool:
    """
    Free, no-network sanity check BEFORE anything touches yfinance or an
    LLM: does this even look like a ticker? NSE symbols are short,
    uppercase, alphanumeric (plus '.', '-', '&' for names like
    'M&M.NS' or 'BAJAJ-AUTO.NS'). This catches "hello world" or a
    pasted sentence immediately, for free - no point spending a
    network call or three LLM turns on input that was never going to
    be a real symbol.

    This is deliberately permissive (nothing here confirms the symbol
    actually EXISTS on NSE) - it just rejects the clearly-not-a-ticker
    cases. Existence is checked separately in symbol_has_market_data().
    """
    return bool(_SYMBOL_SHAPE_RE.match(normalized_symbol))


def symbol_has_market_data(symbol: str) -> bool:
    """
    One cheap, un-cached, un-retried existence probe: does yfinance
    return ANY price data for this symbol at all? Run once, up front,
    before launching the full Research -> Risk -> Trader pipeline.

    Important honesty check on this function: it CANNOT perfectly tell
    "this symbol doesn't exist" apart from "Yahoo happens to be
    rate-limited right this second" - both look like empty data from a
    single call. That ambiguity is fundamental to scraping yfinance,
    not something this function can fully solve. What it DOES do is
    stop obviously-bogus input (a typo, a non-ticker string that still
    passed the shape check, e.g. "ZZZZZ.NS") from burning three LLM
    calls and reaching the price-verification gate in record_trade(),
    where a nonexistent symbol and a real outage are indistinguishable
    for the opposite reason (both return None, and that gate is
    deliberately permissive about None so a real outage doesn't block
    every trade). Catching bad input HERE, before the pipeline starts,
    is strictly better than hoping a downstream gate catches it.
    """
    try:
        import yfinance as yf
        hist = yf.Ticker(symbol).history(period="5d")
        return not hist.empty
    except Exception:
        return False


def profile_file_path(profile_id: str = DEFAULT_PROFILE_ID) -> str:
    """Path to a specific profile's portfolio JSON file (for callers like a reset button)."""
    return os.path.join(PORTFOLIOS_DIR, f"{sanitize_profile_id(profile_id)}.json")


def read_portfolio(profile_id: str = DEFAULT_PROFILE_ID) -> dict:
    """Read a specific profile's portfolio state directly from disk (no agent involved)."""
    path = profile_file_path(profile_id)
    if not os.path.exists(path):
        return {"cash": 500_000.0, "positions": {}, "trade_log": []}
    with open(path, "r") as f:
        return json.load(f)


def _is_tool_leak_bug(err: openai.BadRequestError) -> bool:
    """
    Detects a known bug in gpt-oss models: their internal reasoning
    channel occasionally leaks through as an invalid fake tool call
    (named something like "commentary") instead of a real, correctly
    formatted tool call. This is a model-output quirk, not a genuinely
    invalid request - retrying usually succeeds, since the next
    generation attempt doesn't repeat the same malformed output.
    """
    if isinstance(err.body, dict):
        inner = err.body.get("error", {})
        if isinstance(inner, dict) and inner.get("code") == "tool_use_failed":
            return True
    msg = str(err).lower()
    return "commentary" in msg or "tool call validation failed" in msg


async def _run_with_retry(run_fn, max_attempts: int = 3):
    """
    Runs an agent call, automatically retrying if it hits the known
    tool-leak bug above. Any other error - a real problem - is raised
    immediately, not retried.
    """
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await run_fn()
        except openai.BadRequestError as e:
            if _is_tool_leak_bug(e) and attempt < max_attempts:
                last_error = e
                await asyncio.sleep(1.5 * attempt)
                continue
            raise
    raise last_error


async def run_pipeline_stream(symbol: str, trade_qty: int = DEFAULT_TRADE_QTY, profile_id: str = DEFAULT_PROFILE_ID):
    """
    Run the full Research -> Risk -> Trader pipeline, yielding a progress
    event after each stage starts and finishes.

    profile_id selects which portfolio file is used (data/portfolios/<profile_id>.json),
    so different people/profiles running this don't share one portfolio.

    Yields dicts shaped like:
        {"event": "stage_start", "stage": "research"}
        {"event": "stage_done",  "stage": "research", "output": "..."}
        {"event": "stage_start", "stage": "risk"}
        {"event": "stage_done",  "stage": "risk", "output": "..."}
        {"event": "stage_start", "stage": "trader"}
        {"event": "stage_done",  "stage": "trader", "output": "..."}
        {"event": "complete", "result": {...full dict, same shape as before...}}

    Or, if the symbol is rejected before any agent runs:
        {"event": "invalid_symbol", "symbol": "...", "reason": "..."}
    """
    symbol = normalize_nse_symbol(symbol)
    profile_id = sanitize_profile_id(profile_id)

    # Reject obviously-not-a-symbol input for free, before touching the
    # network or spending any LLM calls on it.
    if not looks_like_a_symbol(symbol):
        yield {
            "event": "invalid_symbol",
            "symbol": symbol,
            "reason": f"'{symbol}' doesn't look like a stock symbol - check for typos or extra characters.",
        }
        return

    # One cheap existence probe before committing to the full pipeline.
    # See symbol_has_market_data()'s docstring for why this can't be
    # a perfect check, only a best-effort front-door filter.
    if not symbol_has_market_data(symbol):
        yield {
            "event": "invalid_symbol",
            "symbol": symbol,
            "reason": f"Could not find any market data for '{symbol}' - it may not be a valid NSE symbol, or Yahoo Finance may be temporarily unavailable. Double-check the symbol and try again.",
        }
        return

    portfolio_env = {"PORTFOLIO_ID": profile_id}

    async with (
        MCPServerStdio(
            params={"command": PYTHON_EXECUTABLE, "args": [MARKET_DATA_SERVER_PATH]},
            name="market-data",
            client_session_timeout_seconds=TIMEOUT_SECONDS,
        ) as market_data_server,
        MCPServerStdio(
            params={"command": PYTHON_EXECUTABLE, "args": [NEWS_SERVER_PATH]},
            name="news",
            client_session_timeout_seconds=TIMEOUT_SECONDS,
        ) as news_server,
        MCPServerStdio(
            params={"command": PYTHON_EXECUTABLE, "args": [MARKET_DATA_SERVER_PATH]},
            name="market-data-price-only",
            client_session_timeout_seconds=TIMEOUT_SECONDS,
            tool_filter=create_static_tool_filter(allowed_tool_names=["get_current_price"]),
        ) as price_only_server,
        MCPServerStdio(
            params={
                "command": PYTHON_EXECUTABLE,
                "args": [PORTFOLIO_SERVER_PATH],
                "env": portfolio_env,
            },
            name="portfolio-readonly",
            client_session_timeout_seconds=TIMEOUT_SECONDS,
            tool_filter=create_static_tool_filter(
                allowed_tool_names=["get_portfolio", "get_position", "check_trade_risk"]
            ),
        ) as portfolio_readonly_server,
        MCPServerStdio(
            params={
                "command": PYTHON_EXECUTABLE,
                "args": [PORTFOLIO_SERVER_PATH],
                "env": portfolio_env,
            },
            name="portfolio-trade",
            client_session_timeout_seconds=TIMEOUT_SECONDS,
            tool_filter=create_static_tool_filter(allowed_tool_names=["record_trade"]),
        ) as portfolio_trade_server,
    ):
        research_agent = build_research_agent(market_data_server, news_server)
        risk_agent = build_risk_agent(portfolio_readonly_server, price_only_server)
        trader_agent = build_trader_agent(portfolio_trade_server)

        yield {"event": "stage_start", "stage": "research"}
        research_result = await _run_with_retry(lambda: Runner.run(
            research_agent,
            f"Research the stock {symbol} and give me your analysis.",
            max_turns=20,
        ))
        research_summary = research_result.final_output
        yield {"event": "stage_done", "stage": "research", "output": research_summary}

        yield {"event": "stage_start", "stage": "risk"}
        risk_result = await _run_with_retry(lambda: Runner.run(
            risk_agent,
            f"Evaluate a proposed trade: BUY {trade_qty} shares of {symbol}. "
            f"Fetch the current price yourself, then check it against portfolio and risk rules. "
            f"State the exact approved quantity clearly in your response.",
            max_turns=15,
        ))
        risk_assessment = risk_result.final_output
        yield {"event": "stage_done", "stage": "risk", "output": risk_assessment}

        yield {"event": "stage_start", "stage": "trader"}
        trader_result = await _run_with_retry(lambda: Runner.run(
            trader_agent,
            f"Research summary:\n{research_summary}\n\n"
            f"Risk assessment:\n{risk_assessment}\n\n"
            f"Make your final trading decision for {symbol}. "
            f"Use the exact quantity from the risk assessment - do not change it.",
            max_turns=15,
        ))
        trader_decision = trader_result.final_output
        yield {"event": "stage_done", "stage": "trader", "output": trader_decision}

    yield {
        "event": "complete",
        "result": {
            "symbol": symbol,
            "profile_id": profile_id,
            "research": research_summary,
            "risk": risk_assessment,
            "trader": trader_decision,
            "portfolio": read_portfolio(profile_id),
        },
    }


async def run_pipeline(symbol: str, trade_qty: int = DEFAULT_TRADE_QTY, profile_id: str = DEFAULT_PROFILE_ID) -> dict:
    """
    Convenience wrapper for callers (like the CLI) that just want the
    final result and don't care about progress events.
    """
    result = None
    async for event in run_pipeline_stream(symbol, trade_qty, profile_id):
        if event["event"] == "complete":
            result = event["result"]
    return result

    
