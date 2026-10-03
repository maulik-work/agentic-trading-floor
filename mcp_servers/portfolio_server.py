"""
Portfolio MCP Server
---------------------
Owns all portfolio state for one profile: cash, positions, trade log.
Exposes read-only tools (get_portfolio, get_position), a deterministic
risk-check tool (check_trade_risk), and the one tool allowed to mutate
state (record_trade).

Two independent safety gates live in record_trade(), both deliberately
NOT delegated to the LLM's judgment:

  1. Risk limits - before any buy executes, record_trade() re-runs
     check_trade_risk() itself as a plain Python call, regardless of what
     the Trader agent claims was already "approved". A hallucinated or
     stale approval can never force an oversized trade through.

  2. Price integrity (added after a real bug: when the Research Agent's
     data lookup silently failed, the Trader had no real price to work
     from and just invented one - a trade executed at a fabricated
     price of 100 for a stock actually worth over 1,000). record_trade()
     now independently fetches the live market price and rejects any
     trade whose submitted price deviates too far from it, instead of
     trusting whatever number the LLM happened to write down.
"""

import os
import re
import json
from datetime import datetime, timezone

import yfinance as yf

STARTING_CASH = 500_000.0
MAX_TRADE_PCT = 0.05       # a single trade can't use more than 5% of portfolio value
MAX_POSITION_PCT = 0.15    # total holding in one symbol can't exceed 15% of portfolio value
PRICE_DEVIATION_TOLERANCE = 0.05  # submitted price can't be off from the live price by more than 5%

from mcp.server import MCPServer

mcp = MCPServer("portfolio")


# ---------------------------------------------------------------------------
# Per-profile storage
# ---------------------------------------------------------------------------
def sanitize_profile_id(raw: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", raw or "")
    return cleaned or "default"


DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "portfolios")
os.makedirs(DATA_DIR, exist_ok=True)

PORTFOLIO_ID = sanitize_profile_id(os.environ.get("PORTFOLIO_ID", "default"))
STATE_FILE = os.path.join(DATA_DIR, f"{PORTFOLIO_ID}.json")


def _default_state() -> dict:
    return {"cash": STARTING_CASH, "positions": {}, "trade_log": []}


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return _default_state()
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return _default_state()


def _save_state(state: dict) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Live price lookup (independent of what any agent reports)
# ---------------------------------------------------------------------------
def _fetch_live_price(symbol: str):
    """
    Returns the latest close price for `symbol`, or None if it can't be
    determined (bad symbol, Yahoo rate-limited, no network, etc). Callers
    must treat None as "could not verify" and decide accordingly - this
    function never raises.
    """
    try:
        hist = yf.Ticker(symbol).history(period="5d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Portfolio valuation
# ---------------------------------------------------------------------------
def _estimate_portfolio_value(state: dict, evaluating_symbol: str = None, evaluating_price: float = None) -> float:
    """
    cash + the value of every held position. For the symbol currently
    being traded, use the live/just-verified price if given. For every
    other held position, use its last-traded price (skipping "hold"
    entries in the trade log, which never set a price the position was
    actually transacted at).
    """
    value = state["cash"]
    for symbol, pos in state["positions"].items():
        qty = pos.get("quantity", 0)
        if qty <= 0:
            continue
        if symbol == evaluating_symbol and evaluating_price is not None:
            price = evaluating_price
        else:
            price = pos.get("last_price", 0)
        value += qty * price
    return value


# ---------------------------------------------------------------------------
# Risk check - pure deterministic math, no LLM judgment involved.
# This is a plain function (not an @mcp.tool) so record_trade() below can
# call it directly in-process as a hard gate, with zero dependency on the
# Trader agent having called the MCP tool version correctly or honestly.
# ---------------------------------------------------------------------------
def _check_trade_risk_logic(symbol: str, action: str, quantity: int, price: float) -> dict:
    state = _load_state()
    portfolio_value = _estimate_portfolio_value(state, evaluating_symbol=symbol, evaluating_price=price)

    trade_value = quantity * price
    trade_pct = trade_value / portfolio_value if portfolio_value else 1.0

    if action == "buy":
        if trade_value > state["cash"]:
            return {
                "approved": False,
                "reason": f"Insufficient cash: trade needs {trade_value:.2f}, only {state['cash']:.2f} available.",
                "suggested_max_quantity": int(state["cash"] / price) if price else 0,
            }

        if trade_pct > MAX_TRADE_PCT:
            max_qty = int((MAX_TRADE_PCT * portfolio_value) / price) if price else 0
            return {
                "approved": False,
                "reason": f"Trade uses {trade_pct*100:.1f}% of portfolio value, exceeding the {MAX_TRADE_PCT*100:.0f}% per-trade limit.",
                "suggested_max_quantity": max_qty,
            }

        existing_qty = state["positions"].get(symbol, {}).get("quantity", 0)
        projected_position_value = (existing_qty + quantity) * price
        position_pct = projected_position_value / portfolio_value if portfolio_value else 1.0
        if position_pct > MAX_POSITION_PCT:
            max_additional_value = max((MAX_POSITION_PCT * portfolio_value) - (existing_qty * price), 0)
            max_qty = int(max_additional_value / price) if price else 0
            return {
                "approved": False,
                "reason": f"Resulting position would be {position_pct*100:.1f}% of portfolio value, exceeding the {MAX_POSITION_PCT*100:.0f}% per-position limit.",
                "suggested_max_quantity": max_qty,
            }

    elif action == "sell":
        held_qty = state["positions"].get(symbol, {}).get("quantity", 0)
        if quantity > held_qty:
            return {
                "approved": False,
                "reason": f"Cannot sell {quantity} shares of {symbol}, only {held_qty} held.",
                "suggested_max_quantity": held_qty,
            }

    return {"approved": True, "reason": "Trade is within risk limits.", "suggested_max_quantity": quantity}


@mcp.tool()
def check_trade_risk(symbol: str, action: str, quantity: int, price: float) -> dict:
    """
    Check whether a proposed buy/sell is within risk limits (5% of
    portfolio value per trade, 15% of portfolio value per position).

    Args:
        symbol: Stock ticker, e.g. "AAPL" or "RELIANCE.NS".
        action: "buy" or "sell".
        quantity: Number of shares proposed.
        price: Proposed execution price per share.
    """
    return _check_trade_risk_logic(symbol, action, quantity, price)


@mcp.tool()
def get_portfolio() -> dict:
    """Get the current cash balance and all held positions."""
    state = _load_state()
    return {"cash": round(state["cash"], 2), "positions": state["positions"]}


@mcp.tool()
def get_position(symbol: str) -> dict:
    """
    Get the current position size for a specific symbol.

    Args:
        symbol: Stock ticker, e.g. "AAPL".
    """
    state = _load_state()
    pos = state["positions"].get(symbol, {"quantity": 0})
    return {"symbol": symbol, "quantity": pos.get("quantity", 0)}


@mcp.tool()
def record_trade(symbol: str, action: str, quantity: int, price: float, reasoning: str) -> dict:
    """
    Record a paper trade (buy/sell/hold) and update the portfolio.
    Independently re-validates both the risk limits and the submitted
    price before executing - the caller's claimed approval is never
    trusted on its own.

    Args:
        symbol: Stock ticker.
        action: "buy", "sell", or "hold".
        quantity: Number of shares (0 for "hold").
        price: Execution price per share (ignored for "hold").
        reasoning: Short explanation of why this trade was made.
    """
    state = _load_state()

    if action == "hold":
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "action": "hold",
            "quantity": 0,
            "price": price,
            "reasoning": reasoning,
        }
        state["trade_log"].append(entry)
        _save_state(state)
        return {"status": "logged", "trade": entry}

    if action not in ("buy", "sell"):
        return {"status": "rejected", "reason": f"Unknown action '{action}'."}

    # --- Gate 1: price integrity -------------------------------------
    # The Trader agent's reported price is never trusted on its own. If
    # Research's own price lookup failed upstream, the agent has nothing
    # real to anchor on and may invent a number - this catches that
    # before it can ever be written to the portfolio.
    live_price = _fetch_live_price(symbol)
    if live_price is not None:
        deviation = abs(price - live_price) / live_price if live_price else 1.0
        if deviation > PRICE_DEVIATION_TOLERANCE:
            return {
                "status": "rejected",
                "reason": (
                    f"Submitted price {price} for {symbol} deviates "
                    f"{deviation*100:.1f}% from the independently verified "
                    f"live price {live_price:.2f} - rejected as a likely "
                    f"hallucinated or stale price rather than executed."
                ),
            }
        # Anchor execution to the verified live price rather than the
        # LLM's own number, even when it was close but not exact.
        price = round(live_price, 2)
    # If live_price is None (e.g. Yahoo rate-limited right now), we can't
    # verify independently. Rather than blocking every trade whenever the
    # market-data API has a bad moment, we fall through and trust the
    # agent's price for this one trade - but it's flagged in the log.
    price_verified = live_price is not None

    # --- Gate 2: risk limits -------------------------------------------
    risk = _check_trade_risk_logic(symbol, action, quantity, price)
    if not risk["approved"]:
        return {
            "status": "rejected",
            "reason": f"Blocked by risk check: {risk['reason']}",
            "suggested_max_quantity": risk["suggested_max_quantity"],
        }

    # --- Execute ---------------------------------------------------------
    trade_value = quantity * price
    if action == "buy":
        state["cash"] -= trade_value
        pos = state["positions"].setdefault(symbol, {"quantity": 0, "last_price": price})
        pos["quantity"] += quantity
        pos["last_price"] = price
    else:  # sell
        pos = state["positions"].setdefault(symbol, {"quantity": 0, "last_price": price})
        pos["quantity"] -= quantity
        pos["last_price"] = price
        state["cash"] += trade_value
        if pos["quantity"] <= 0:
            state["positions"].pop(symbol, None)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "price": price,
        "reasoning": reasoning,
        "price_verified": price_verified,
    }
    state["trade_log"].append(entry)
    _save_state(state)

    return {"status": "executed", "trade": entry, "remaining_cash": round(state["cash"], 2)}


if __name__ == "__main__":
    mcp.run()
    
