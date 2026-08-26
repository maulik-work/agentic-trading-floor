# Agentic Trading Floor (v3 — deployed)

A multi-agent system that researches an NSE stock (price + news), checks
a proposed trade against portfolio risk rules, and executes a paper
trade — built with the OpenAI Agents SDK and MCP, running on Groq's
free hosted LLM API, deployable for free on Streamlit Community Cloud.

## Architecture

```
Research Agent ──uses──> market-data (full: price/history/company info)
     │                   news (headlines)
     │ summary
     ▼
Risk Agent ──uses──> market-data-price-only (get_current_price only)
     │                portfolio-readonly (get_portfolio, get_position,
     │                                    check_trade_risk)
     │ approval + exact quantity
     ▼
Trader Agent ──uses──> portfolio-trade (record_trade ONLY - buy/sell/hold)
```

**Why each agent gets a different, narrow slice of tools:** models get
noticeably less reliable at producing correctly-structured tool calls
as the number of available tools grows. Giving each agent only what it
needs — not just for security, but for raw reliability — fixed several
real bugs during development.

**Why `record_trade` re-checks risk itself:** an agent's stated approval
is not a security boundary. `record_trade` independently re-runs
`check_trade_risk` before executing any buy, so even if an agent
proposes a bad quantity, the trade is rejected at the actual point of
execution — not just flagged in a conversation.

**Hold decisions are logged too:** every decision (buy, sell, or hold)
goes through `record_trade`, so there's a full history of what the
system considered even when it chose not to trade.

## Setup (local)

```bash
uv init --python 3.12
uv add openai-agents mcp yfinance feedparser python-dotenv streamlit pandas
```

Get a free Groq API key (no credit card): https://console.groq.com/keys

Create a `.env` file in the project root:
```
GROQ_API_KEY=your-groq-key-here
```

## Run — CLI

```bash
uv run app\main.py OLAELEC       # NSE symbols only, .NS added automatically
uv run app\main.py RELIANCE
```

## Run — Dashboard (recommended)

```bash
uv run streamlit run app\dashboard.py
```

Opens a browser tab: enter an NSE symbol, click "Run Analysis", and
watch live progress as each agent runs, then see Research/Risk/Trader
output in separate tabs, plus a sidebar showing portfolio cash,
positions, and recent decisions (including holds).

Portfolio state persists in `data/portfolio.json` between runs,
starting with ₹1,00,000 paper cash. Delete that file, or use the
"Reset Portfolio" button in the sidebar, to start fresh.

## Deployment (Streamlit Community Cloud - free, public URL)

1. Push this project to a GitHub repo (the `.gitignore` already excludes
   `.env`, `data/portfolio.json`, and other local-only files - never
   commit your actual API key).
2. Go to https://share.streamlit.io, sign in with GitHub, click
   "New app", and point it at your repo with `app/dashboard.py` as the
   main file.
3. In the app's Settings → Secrets, add:
   ```
   GROQ_API_KEY = "your-groq-key-here"
   ```
4. Deploy. You'll get a permanent `https://<something>.streamlit.app`
   URL, reachable from any phone or browser, with no laptop needing to
   stay on.

**Why Groq instead of Ollama for this:** Ollama needs to run on the
same machine as the code. Free hosting platforms have no way to run it
for you. Groq's free tier is a genuinely no-cost, OpenAI-compatible
hosted API (~30 requests/minute), which is exactly enough for personal
or demo use - the tradeoff is your queries now leave your machine and
go to Groq's servers, reasonable for a paper-trading demo project.

## Known limitations (intentional, for a project at this stage)

- **Portfolio valuation for held symbols other than the one being traded**
  uses the last recorded trade price, not a live quote.
- **Single-run pipeline** — no scheduled/looped execution across a
  watchlist. Each run evaluates exactly one symbol once.
- **No short-selling or leverage logic** — only long positions.
- **Groq's free tier rate limits** (~30 requests/minute) mean this isn't
  built for heavy concurrent traffic - fine for personal/demo use.

## Roadmap ideas

- Loop across a watchlist on a schedule
- Portfolio value chart over time (from `data/portfolio.json`'s trade log)
- Live Kafka feed as an alternative to yfinance for market data
