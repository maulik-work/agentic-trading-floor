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

PYTHON_EXECUTABLE = sys.executable
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(REPO_ROOT)

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


def profile_file_path(profile_id: str = DEFAULT_PROFILE_ID) -> str:
    """Path to a specific profile's portfolio JSON file (for callers like a reset button)."""
    return os.path.join(PORTFOLIOS_DIR, f"{sanitize_profile_id(profile_id)}.json")


def read_portfolio(profile_id: str = DEFAULT_PROFILE_ID) -> dict:
    """Read a specific profile's portfolio state directly from disk (no agent involved)."""
    path = profile_file_path(profile_id)
    if not os.path.exists(path):
        return {"cash": 100_000.0, "positions": {}, "trade_log": []}
    with open(path, "r") as f:
        return json.load(f)


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
    """
    symbol = normalize_nse_symbol(symbol)
    profile_id = sanitize_profile_id(profile_id)
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
        research_result = await Runner.run(
            research_agent,
            f"Research the stock {symbol} and give me your analysis.",
        )
        research_summary = research_result.final_output
        yield {"event": "stage_done", "stage": "research", "output": research_summary}

        yield {"event": "stage_start", "stage": "risk"}
        risk_result = await Runner.run(
            risk_agent,
            f"Evaluate a proposed trade: BUY {trade_qty} shares of {symbol}. "
            f"Fetch the current price yourself, then check it against portfolio and risk rules. "
            f"State the exact approved quantity clearly in your response.",
        )
        risk_assessment = risk_result.final_output
        yield {"event": "stage_done", "stage": "risk", "output": risk_assessment}

        yield {"event": "stage_start", "stage": "trader"}
        trader_result = await Runner.run(
            trader_agent,
            f"Research summary:\n{research_summary}\n\n"
            f"Risk assessment:\n{risk_assessment}\n\n"
            f"Make your final trading decision for {symbol}. "
            f"Use the exact quantity from the risk assessment - do not change it.",
        )
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
