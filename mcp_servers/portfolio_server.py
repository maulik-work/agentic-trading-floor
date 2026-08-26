"""
Portfolio MCP Server
---------------------
Tracks a simple paper-trading portfolio in a local JSON file.
Includes a deterministic risk-check tool (real arithmetic, not LLM math)
and a hard execution-time gate: record_trade re-validates every buy
against risk limits itself, regardless of what the caller claims.

Which portfolio file is used depends on the PORTFOLIO_ID environment
variable, set by the launching process (pipeline.py) - this is what lets
different people/profiles have separate portfolios when this is deployed
publicly, instead of everyone sharing one shared state.
"""

import json
import os
import re
from datetime import datetime, timezone
from mcp.server import MCPServer

mcp = MCPServer("portfolio")

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "portfolios")


def _safe_profile_id(raw: str) -> str:
    """Sanitize a profile id to a safe filename component (alnum, dash, underscore only)."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "", raw or "").strip()
    return cleaned or "default"


PORTFOLIO_ID = _safe_profile_id(os.environ.get("PORTFOLIO_ID", "default"))
STATE_FILE = os.path.join(DATA_DIR, f"{PORTFOLIO_ID}.json")
STARTING_CASH = 500_000.0
MAX_TRADE_PCT = 0.05     # no single trade may exceed 5% of total portfolio value
MAX_POSITION_PCT = 0.15  # no single position may exceed 15% of total portfolio value


def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        state = {"cash": STARTING_CASH, "positions": {}, "trade_log": []}
        _save_state(state)
        return state
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def _save_state(state: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _estimate_portfolio_value(state: dict, symbol: str, current_price: float) -> float:
    """
    Estimate total portfolio value: cash + value of all positions.
    Uses current_price for the given symbol, and the most recent recorded
    trade price for any other held symbols (an approximation, not live
    mark-to-market, since this server doesn't fetch prices for other symbols).
    """
    total = state["cash"]
    for held_symbol, qty in state["positions"].items():
        if held_symbol == symbol:
            total += qty * current_price
        else:
            last_price = None
            for trade in reversed(state["trade_log"]):
                if trade["symbol"] == held_symbol and trade["action"] != "hold":
                    last_price = trade["price"]
                    break
            total += qty * (last_price or 0)
    return total


@mcp.tool()
def get_portfolio() -> dict:
    """Get the current cash balance and all open positions."""
    return _load_state()


@mcp.tool()
def get_position(symbol: str) -> dict:
    """
    Get the current position size for a specific symbol.

    Args:
        symbol: Stock ticker, e.g. "AAPL".
    """
    state = _load_state()
    qty = state["positions"].get(symbol, 0)
    return {"symbol": symbol, "quantity": qty}


@mcp.tool()
def check_trade_risk(symbol: str, action: str, quantity: int, price: float) -> dict:
    """
    Deterministically evaluate a proposed trade against risk rules.
    Does the math in code, not via LLM reasoning - use this instead of
    calculating percentages yourself.

    Args:
        symbol: Stock ticker.
        action: "buy" or "sell".
        quantity: Number of shares proposed.
        price: Current market price per share.
    """
    if price <= 0 or quantity <= 0:
        return {"approved": False, "reason": f"Invalid price ({price}) or quantity ({quantity})."}

    state = _load_state()
    action = action.lower().strip()
    trade_value = quantity * price
    portfolio_value = _estimate_portfolio_value(state, symbol, price)

    if portfolio_value <= 0:
        return {"approved": False, "reason": "Cannot evaluate: portfolio value is zero or negative."}

    trade_pct = trade_value / portfolio_value
    if trade_pct > MAX_TRADE_PCT:
        max_qty = int((MAX_TRADE_PCT * portfolio_value) / price)
        return {
            "approved": False,
            "reason": f"Trade is {trade_pct:.1%} of portfolio value, exceeds {MAX_TRADE_PCT:.0%} limit.",
            "suggested_max_quantity": max_qty,
        }

    if action == "buy":
        current_qty = state["positions"].get(symbol, 0)
        new_position_value = (current_qty + quantity) * price
        position_pct = new_position_value / portfolio_value
        if position_pct > MAX_POSITION_PCT:
            max_total_qty = int((MAX_POSITION_PCT * portfolio_value) / price)
            max_additional_qty = max(0, max_total_qty - current_qty)
            return {
                "approved": False,
                "reason": f"Resulting position would be {position_pct:.1%} of portfolio, exceeds {MAX_POSITION_PCT:.0%} limit.",
                "suggested_max_quantity": max_additional_qty,
            }
        if trade_value > state["cash"]:
            return {"approved": False, "reason": f"Insufficient cash: need {trade_value}, have {state['cash']}."}

    elif action == "sell":
        held = state["positions"].get(symbol, 0)
        if quantity > held:
            return {"approved": False, "reason": f"Insufficient shares: trying to sell {quantity}, hold {held}."}

    return {
        "approved": True,
        "reason": f"Trade is {trade_pct:.1%} of portfolio value, within limits.",
        "trade_value": trade_value,
        "portfolio_value": portfolio_value,
    }


@mcp.tool()
def record_trade(symbol: str, action: str, quantity: int, price: float, reasoning: str) -> dict:
    """
    Record a trading decision - buy, sell, or hold - and update the
    portfolio if it's a buy or sell. Always call this tool for every
    decision, including hold, so there's a full history of what was
    considered even when no trade happened.

    Buys are validated against risk limits before execution - a buy that
    violates the trade-size or position-size rules will be rejected here,
    even if the caller claims it was already approved.

    Args:
        symbol: Stock ticker.
        action: "buy", "sell", or "hold".
        quantity: Number of shares (use 0 for hold).
        price: Current price per share (used for context on hold; the
            execution price for buy/sell).
        reasoning: Short explanation of why this decision was made.
    """
    state = _load_state()
    action = action.lower().strip()

    if action == "hold":
        decision_record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "action": "hold",
            "quantity": 0,
            "price": price if price and price > 0 else None,
            "reasoning": reasoning,
        }
        state["trade_log"].append(decision_record)
        _save_state(state)
        return {"status": "logged", "decision": decision_record}

    cost = quantity * price

    if price <= 0:
        return {"status": "rejected", "reason": f"Invalid price: {price}. Price must be greater than 0."}
    if quantity <= 0:
        return {"status": "rejected", "reason": f"Invalid quantity: {quantity}. Quantity must be greater than 0."}

    if action == "buy":
        risk_check = check_trade_risk(symbol, action, quantity, price)
        if not risk_check["approved"]:
            return {
                "status": "rejected",
                "reason": f"Risk check failed at execution time: {risk_check['reason']}",
                "suggested_max_quantity": risk_check.get("suggested_max_quantity"),
            }
        if cost > state["cash"]:
            return {"status": "rejected", "reason": "Insufficient cash", "cash_available": state["cash"]}
        state["cash"] -= cost
        state["positions"][symbol] = state["positions"].get(symbol, 0) + quantity
    elif action == "sell":
        held = state["positions"].get(symbol, 0)
        if quantity > held:
            return {"status": "rejected", "reason": "Insufficient shares held", "shares_held": held}
        state["cash"] += cost
        state["positions"][symbol] = held - quantity
        if state["positions"][symbol] == 0:
            del state["positions"][symbol]
    else:
        return {"status": "rejected", "reason": f"Unknown action '{action}', use 'buy', 'sell', or 'hold'"}

    trade_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "action": action,
        "quantity": quantity,
        "price": price,
        "reasoning": reasoning,
    }
    state["trade_log"].append(trade_record)
    _save_state(state)
    return {"status": "executed", "trade": trade_record, "remaining_cash": state["cash"]}


@mcp.tool()
def get_trade_log(limit: int = 10) -> dict:
    """
    Get the most recent trades executed.

    Args:
        limit: Max number of recent trades to return.
    """
    state = _load_state()
    return {"trades": state["trade_log"][-limit:]}


if __name__ == "__main__":
    mcp.run(transport="stdio")
