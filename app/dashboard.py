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
from pipeline import run_pipeline_stream, read_portfolio, profile_file_path

st.set_page_config(page_title="Agentic Trading Floor", page_icon="📈", layout="wide")

st.title("📈 Agentic Trading Floor")
st.caption(
    "A 3-agent system (Research → Risk → Trader) built with the OpenAI Agents SDK "
    "and MCP. Paper trading only - no real money."
)

# ---- Profile selection: each name gets its own separate portfolio.
# Reflected in the URL (?profile=...) so a person can bookmark/share their own link.
default_profile = st.query_params.get("profile", "default")

# ---- Sidebar: profile picker + portfolio snapshot ----
with st.sidebar:
    st.header("Your Profile")
    profile_id = st.text_input(
        "Profile name",
        value=default_profile,
        help="Use the same name each time to keep your own separate portfolio. "
        "Different names never share data.",
    )
    if not profile_id:
        profile_id = "default"
    st.query_params["profile"] = profile_id

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

    with tab2:
        st.markdown(result["risk"])

    with tab3:
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
