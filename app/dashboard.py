"""
Agentic Trading Floor - Dashboard
-------------------------------------
A readable, visual interface for the trading pipeline. Runs on Groq's
free hosted LLM API, so this works fully deployed (e.g. on Streamlit
Community Cloud) - no local model, no laptop needing to stay on.

Usage (local):
    uv run streamlit run app\\dashboard.py
"""

import os
import asyncio
import streamlit as st

# Bridge Streamlit Cloud's secrets into the environment BEFORE importing
# pipeline (which imports the agents, which import model_config.py -
# that module reads GROQ_API_KEY from the environment at import time).
# Wrapped in try/except: st.secrets raises if no secrets.toml exists at
# all, which is the normal case for local dev using a .env file instead.
try:
    if "GROQ_API_KEY" in st.secrets:
        os.environ["GROQ_API_KEY"] = st.secrets["GROQ_API_KEY"]
except Exception:
    pass  # no secrets.toml - fine locally, GROQ_API_KEY should come from .env instead

import pandas as pd
from pipeline import run_pipeline_stream, read_portfolio, profile_file_path, sanitize_profile_id

st.set_page_config(page_title="Agentic Trading Floor", page_icon="📈", layout="wide")

st.title("📈 Agentic Trading Floor")

with st.expander("👋 What is this?", expanded=True):
    st.markdown(
        """
Hey, I'm Maulik — I built this to solve something that bugged me: if you
just ask an AI "should I buy this stock?", it gives you the same generic
answer it'd give anyone. But whether a trade actually makes sense
depends on *your* situation — buying 10 shares is nothing if it's 3% of
your portfolio, but reckless if it's 40%.

So instead of one AI trying to do everything, I built three that each
handle one part of the job, the way a real trading desk splits up work:

- **Research** reads the stock's recent price action and news, and
  writes up a plain-English take — no numbers-speak.
- **Risk** checks whatever's proposed against your actual portfolio and
  hard position limits. This one's a bit different from the others: I
  don't let the AI do this math itself — it's real code doing the
  arithmetic, because I found LLMs will confidently get percentages
  wrong. The AI's job here is just to read the tool's answer back to you.
- **Trader** takes both and makes the final call — buy, sell, or hold —
  and every decision gets logged, even the holds, so there's a record
  of what it considered.

It's running on a free hosted model (Groq), so this costs nothing to
run, and it trades with fake money only — nothing here touches anything
real. Pick a profile name below (so your portfolio stays separate from
anyone else using this link), type in an NSE stock symbol, and watch it
work.
"""
    )

# ---- Profile gate ----
# A fresh visit (no ?profile= in the URL) must pick a name before seeing
# the app. A link that already has ?profile= (e.g. a bookmarked or shared
# link) skips straight in - that's what makes "send someone their own
# link" work smoothly.
if "profile_confirmed" not in st.session_state:
    st.session_state.profile_confirmed = "profile" in st.query_params

if not st.session_state.profile_confirmed:
    st.subheader("Pick a profile name to get started")
    st.caption(
        "This keeps your paper-trading portfolio separate from anyone else "
        "using this link - like a username. There's no password though, so "
        "don't use a name a stranger could easily guess if you want it kept "
        "just to yourself."
    )
    entered = st.text_input("Profile name", placeholder="e.g. dad, maulik, guest42")

    if st.button("Check availability"):
        if entered.strip():
            st.session_state.checked_name = entered
            st.session_state.checked_exists = os.path.exists(profile_file_path(entered))
        else:
            st.warning("Type a name first.")

    if st.session_state.get("checked_name") == entered and entered.strip():
        clean_name = sanitize_profile_id(entered)
        if st.session_state.checked_exists:
            st.info(f"👋 Welcome back — **{clean_name}** already has a portfolio. Continuing will load it.")
        else:
            st.success(f"✅ **{clean_name}** is free — a new ₹1,00,000 portfolio will be created for it.")
        if st.button("Continue →", type="primary"):
            st.query_params["profile"] = clean_name
            st.session_state.profile_confirmed = True
            st.rerun()

    st.stop()  # don't render the rest of the app until a profile is confirmed

profile_id = sanitize_profile_id(st.query_params.get("profile", "default"))

# ---- Sidebar: current profile + portfolio snapshot ----
with st.sidebar:
    st.header("Your Profile")
    st.markdown(f"**{profile_id}**")
    if st.button("Switch profile"):
        st.session_state.profile_confirmed = False
        st.query_params.clear()
        st.rerun()

    st.divider()
    st.header("Portfolio")
    portfolio = read_portfolio(profile_id)
    st.metric("Cash", f"₹{portfolio['cash']:,.2f}")

    if portfolio["positions"]:
        st.subheader("Positions")
        pos_df = pd.DataFrame(
            [{"Symbol": sym, "Shares": qty} for sym, qty in portfolio["positions"].items()]
        )
        st.dataframe(pos_df, hide_index=True, width="stretch")
    else:
        st.info("No open positions yet.")

    if portfolio["trade_log"]:
        st.subheader("Recent Decisions")
        st.caption("Includes trades executed and holds (no trade made).")
        decisions_df = pd.DataFrame(portfolio["trade_log"][-10:][::-1])
        action_display = {"buy": "🟢 buy", "sell": "🔴 sell", "hold": "⚪ hold"}
        decisions_df["action"] = decisions_df["action"].map(action_display).fillna(decisions_df["action"])
        st.dataframe(
            decisions_df[["timestamp", "symbol", "action", "quantity", "price", "reasoning"]],
            hide_index=True,
            width="stretch",
        )

    if st.button("Reset Portfolio", help=f"Deletes '{profile_id}' history and resets to ₹1,00,000 cash"):
        path = profile_file_path(profile_id)
        if os.path.exists(path):
            os.remove(path)
        st.rerun()

# ---- Main area: run the pipeline ----
col1, col2 = st.columns([3, 1])
with col1:
    symbol = st.text_input(
        "NSE stock symbol",
        placeholder="e.g. OLAELEC, RELIANCE, TCS, IONEXCHANG",
        help="Just enter the NSE symbol - .NS is added automatically",
    )
with col2:
    st.write("")
    st.write("")
    run_clicked = st.button("Run Analysis", type="primary", width="stretch")

STAGE_LABELS = {
    "research": "🔍 Research agent — analyzing price data and news...",
    "risk": "⚖️ Risk agent — checking portfolio and position limits...",
    "trader": "💼 Trader agent — making the final decision...",
}

if run_clicked and symbol:
    async def run_and_track(status_box):
        final_result = None
        async for event in run_pipeline_stream(symbol, profile_id=profile_id):
            if event["event"] == "stage_start":
                status_box.update(label=STAGE_LABELS[event["stage"]], state="running")
            elif event["event"] == "stage_done":
                st.write(f"✅ {event['stage'].capitalize()} done")
            elif event["event"] == "complete":
                final_result = event["result"]
        return final_result

    with st.status("Starting pipeline...", expanded=True) as status_box:
        try:
            result = asyncio.run(run_and_track(status_box))
        except Exception as e:
            status_box.update(label="Pipeline failed", state="error")
            st.error(f"Pipeline failed: {e}")
            st.stop()
        status_box.update(label=f"Analysis complete for {result['symbol']}", state="complete")

    tab1, tab2, tab3 = st.tabs(["🔍 Research", "⚖️ Risk Check", "💼 Trader Decision"])

    with tab1:
        st.markdown(result["research"])
        with st.expander("📊 What data feeds this analysis?"):
            st.markdown(
                """
This isn't a single "ask the AI" call — the agent pulls five real,
separate pieces of data before writing anything:

**1. Current price** — live, from the exchange.

**2. Moving averages (50-day and 200-day)** — the average closing price
over the last 50 and 200 trading days. Why this matters: a single
day's price can jump around for no real reason, but if the price has
stayed *above* its 200-day average for months, that's a genuine,
harder-to-fake trend signal. When the 50-day average crosses **below**
the 200-day average, that's called a **death cross** — a classic
bearish signal traders watch for. The reverse (50-day crossing above)
is a **golden cross** — bullish.

**3. Fundamentals** — revenue growth, earnings growth, debt-to-equity,
and return on equity, pulled from the company's actual reported
financials, not estimated. This is what separates "the stock went up"
from "the business is actually doing well" — a stock can rise on hype
while the underlying numbers are getting worse, which is exactly the
kind of mismatch this is meant to catch.

**4. Company basics** — sector, market cap, 52-week high/low, for context.

**5. Recent news** — real headlines, pulled live, not the AI's memory
of old training data.

**What the AI actually does with all this:** synthesize it into
plain English and a verdict. It doesn't invent any of the five numbers
above — those come from real tool calls to Yahoo Finance and Google
News. The AI's job is judgment and writing, not data collection.

**Honest limitation:** this is still a simplified signal compared to
what a professional analyst uses — no peer/sector comparison, no
analyst price targets, no options-market sentiment. Good enough to
demonstrate real technical + fundamental analysis working together,
not a replacement for real due diligence.
"""
            )

    with tab2:
        st.markdown(result["risk"])
        with st.expander("🧮 How is this calculated?"):
            st.markdown(
                """
This isn't the AI doing mental math — the percentages above come from a
plain Python function, so they're exact, not estimated.

**Trade size check:**
trade_value = quantity × price
trade_% = trade_value / total_portfolio_value
If `trade_%` is over **5%**, the trade is rejected — a single trade can
never risk more than 5% of everything you have.

**Position size check** (for buys):
new_position_value = (shares_already_held + new_quantity) × price
position_% = new_position_value / total_portfolio_value
If `position_%` is over **15%**, it's rejected — no single stock can
ever end up being more than 15% of your total portfolio, even built up
gradually across several trades.

**Total portfolio value** itself is:
cash + value of every open position (at current or last-known price)

If a trade is rejected, the system also calculates the largest quantity
that *would* pass — that's real algebra solving for quantity, not a
guess.

I built it this way on purpose: I don't trust an AI to get percentages
right every time — I found it occasionally would confidently state a
wrong number. So the actual limit-checking is code, not language model
output. The AI's only job here is to read the result back to you in
plain English.
"""
            )
    with tab3:
        # Pull the reasoning straight from the trade log entry the agent
        # just wrote - this is guaranteed to exist (record_trade requires
        # it), unlike relying on the model's free-text response alone.
        updated_portfolio = read_portfolio(profile_id)
        latest_decision = updated_portfolio["trade_log"][-1] if updated_portfolio["trade_log"] else None

        if latest_decision and latest_decision["symbol"] == result["symbol"]:
            action_icon = {"buy": "🟢", "sell": "🔴", "hold": "⚪"}.get(latest_decision["action"], "")
            st.markdown(f"### {action_icon} {latest_decision['action'].upper()} — Why?")
            st.info(latest_decision["reasoning"])
            if latest_decision["action"] != "hold":
                st.caption(f"Quantity: {latest_decision['quantity']} shares · Price: ₹{latest_decision['price']}")
            st.divider()

        st.markdown(result["trader"])
        if "DECISION: hold" in result["trader"]:
            st.info("No trade was executed - decision was to hold.")
        else:
            st.success("Trade processed - check the sidebar for updated portfolio state.")

elif run_clicked and not symbol:
    st.warning("Enter a stock symbol first.")

st.divider()
st.caption(
    "⚠️ Paper trading demo only. Not financial advice. Research signal is based on "
    "5-day price history and recent news headlines - intentionally simplified for "
    "this project; see README for details."
)
