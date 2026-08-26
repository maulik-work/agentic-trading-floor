"""
Risk Agent
-----------
Job: enforce risk rules on any proposed trade before it happens, using a
deterministic tool for the actual math rather than doing arithmetic itself.
Deliberately given a MINIMAL tool set (get_current_price + check_trade_risk
only) - fewer available tools makes small local models much more reliable
at producing correctly-structured tool calls.
"""

from agents import Agent
from trading_agents.model_config import local_model


def build_risk_agent(portfolio_mcp_server, price_only_mcp_server) -> Agent:
    return Agent(
        name="Risk Agent",
        model=local_model,
        instructions="""
You are a risk manager. Given a proposed trade (symbol, action, quantity):

STEP 1: Use your price tool to get the current price for the symbol.

STEP 2: Call the check_trade_risk tool with the symbol, action, quantity,
and the current price you just fetched. Do NOT calculate percentages or
portfolio value yourself - the tool does this calculation for you.

STEP 3: Report the tool's result clearly: "APPROVED" or "REJECTED",
followed by the tool's reason. If REJECTED and the tool gives a
suggested_max_quantity, mention that suggested quantity explicitly.

Note: even if you approve a trade, the trade execution tool independently
re-checks these same limits before executing - so your job is to give an
accurate read, not to be the only safeguard.
""",
        mcp_servers=[portfolio_mcp_server, price_only_mcp_server],
    )
