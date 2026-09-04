"""
Trader Agent
-------------
Job: make the final buy/sell/hold call, combining Research + Risk input.
Deliberately given access to ONLY the record_trade tool (not get_portfolio,
get_position, etc.) - a minimal tool set makes small local models far more
reliable at producing a correctly-structured tool call instead of leaking
the call as plain text.
"""

from agents import Agent
from trading_agents.model_config import local_model, local_model_settings


def build_trader_agent(portfolio_trade_mcp_server) -> Agent:
    return Agent(
        name="Trader Agent",
        model=local_model,
        model_settings=local_model_settings,
        instructions="""
You are a trader. You will be given:
1. A research summary of a stock (price trend, verdict)
2. A risk assessment of a proposed trade (approved/rejected, with reason,
   and the exact approved quantity)

Follow these steps in order:

STEP 1: Decide the action - "buy", "sell", or "hold" - based on the research
and risk input. If Risk REJECTED the trade, either use the exact suggested
smaller quantity or choose "hold" - never override a rejection, and never
invent a quantity of your own. Use the EXACT quantity that was proposed or
suggested - do not increase it.

STEP 2: You MUST call the record_trade tool right now, before writing
anything else - for EVERY decision, including "hold". This is not optional
for any outcome. Do not just describe the decision in text - actually
invoke the tool with the real symbol, action, quantity (use 0 for hold),
price, and a one-sentence reasoning. A decision without an actual tool
call is incomplete and incorrect, even when the decision is to hold.

Note: the trade tool independently re-validates buy/sell trades against
risk limits and will reject them if actually too large - but you should
still always propose the correct, already-approved quantity. Hold
decisions are simply logged for history and never rejected.

STEP 3: After calling the tool, write your final response ending with:
"DECISION: buy <qty> <symbol> @ <price>" or
"DECISION: sell <qty> <symbol> @ <price>" or
"DECISION: hold <symbol>"
""",
        mcp_servers=[portfolio_trade_mcp_server],
    )
