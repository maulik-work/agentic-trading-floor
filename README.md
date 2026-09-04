# Agentic Trading Floor

A multi-agent stock research and paper-trading system for NSE stocks, built with the OpenAI Agents SDK and MCP, deployed publicly on Streamlit Community Cloud.

**Live demo:** agentic-trading-floor-maulik.streamlit.app
**Stack:** Python, OpenAI Agents SDK, MCP, Groq, Streamlit, yFinance

## Why I built this

If you just ask an LLM "should I buy this stock," it gives you generic advice — the same answer no matter who's asking, because it has no idea what your actual portfolio looks like. But the right call really depends on that. Buying 10 shares is fine if it's 3% of your holdings, reckless if it's 40%. A single LLM call also has no access to real, current data, and — this is the part that actually shaped the whole design — I found LLMs are genuinely unreliable at doing precise arithmetic, even when they sound completely confident about it.

So instead of one AI trying to do everything, I split the problem into three agents that each do one job, roughly the way a real trading desk splits up work:

- **Research** pulls live price data, technical indicators (RSI, moving averages, golden/death cross), company fundamentals, and news, and writes up a plain-English read on the stock.
- **Risk** checks whatever's proposed against my actual portfolio — 5% max per trade, 15% max per position. The risk math itself isn't done by the LLM at all, it's a dedicated tool that does real arithmetic, because I didn't trust the model to get percentages right every time.
- **Trader** takes both and makes the final call — buy, sell, or hold — and every decision gets logged with its reasoning, even the holds.

It's all fake money. Nothing here touches anything real.

## How the agents actually talk to each other and their tools

Each agent connects to its tools over MCP (Model Context Protocol) instead of just calling functions directly. I have three MCP servers — `market_data_server.py`, `news_server.py`, and `portfolio_server.py` — each running as its own subprocess.

The part I think is actually interesting here: `portfolio_server.py` gets launched **twice**, as two separate connections with different tool restrictions. Risk gets a read-only connection (it can check the portfolio and run the risk calculation, but can't touch anything). Trader gets a connection that only exposes one tool — `record_trade`. Same underlying code, two genuinely different permission levels, because MCP separates the client (the connection) from the server (the actual code).

I didn't originally plan this as a security thing. I found it because I kept hitting a bug where the model would leak a tool call out as garbled text instead of actually calling it — and it got worse the more tools an agent had access to. Cutting each agent down to only the tools it strictly needs fixed that bug and, as a side effect, meant no agent has more access than its job actually requires.

## The bug I'm most glad I caught

At one point, Risk would approve a trade of, say, 10 shares — but Trader would sometimes execute 100 instead. It wasn't reading the approved number carefully, it was just generating a plausible-looking one. In a system that's supposedly enforcing risk limits, that's a real problem, not a cosmetic one.

I fixed the prompt (explicit numbered steps, "use the exact quantity, never invent your own"), but I didn't stop there, because I don't think a prompt fix alone is a real fix. I made the actual trade-execution function (`record_trade`) independently re-run the risk check itself, right before touching any money, regardless of what the Trader agent claims was approved. So even if a model slips up again in the future, or I swap in a different model that behaves differently, an oversized trade still can't go through — the safety boundary lives in code, not in hoping the AI behaves.

## Why Groq instead of a local model

I built and tested this on Ollama, running the model on my own machine — free, and fine for development. But Ollama only exists on the machine running it. Once I wanted this to actually be a website someone else could open — not just something on my laptop — that stopped working, since free hosting platforms can't run Ollama for you.

Groq gives free, hosted access to open models through an API that's built to look like OpenAI's — same client code, I just pointed it at a different URL. That's what let me deploy this for real, on Streamlit Community Cloud, at zero cost.

Along the way I also hit a model that Groq deprecated mid-project, and separately, a documented bug in the `gpt-oss` model family where its internal response formatting would leak through as an invalid tool call once my tool list got big enough. Both times the fix wasn't in my code — it was recognizing the error wasn't mine to fix and picking a different model.

## Letting more than one person use it

Once this was a public link, everyone hitting it would've shared one portfolio by default, which defeats the point. Each profile name now gets its own portfolio file, selected by an environment variable passed to the portfolio subprocess when it launches. It's not real authentication — there's no password, "taken" just means a portfolio already exists under that name — but it's enough for what this actually needs to be: a way for me, my dad, or anyone else with the link to each have their own fake ₹5,00,000 without stepping on each other's trades.

## What this doesn't do

- No short selling or leverage — only long positions, and the research signal (5-day-ish price history, a handful of news headlines) is intentionally simple, not something I'd actually trade on
- No scheduled/looped runs — it's one symbol, one run, on demand
- Streamlit Community Cloud's free tier has an ephemeral filesystem, so portfolios can reset if the app sleeps or gets redeployed — fine for fake money, not something I'd rely on for anything real
- Groq's free tier has a real rate limit (~30 requests/minute), which is why I added retry logic — fine for personal use, not built for heavy traffic

## What I'd actually add next

Short selling with proper margin requirements, a scheduled watchlist instead of one symbol at a time, and probably a real database instead of JSON files once the ephemeral-filesystem limitation actually matters.
