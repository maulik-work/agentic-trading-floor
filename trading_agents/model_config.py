"""
Model config - uses Groq's free, hosted, OpenAI-compatible API instead
of a local Ollama instance. Switched from Ollama specifically to enable
public deployment (Streamlit Community Cloud can't run Ollama for you,
since there's no local machine to host the model on).

Free tier: no credit card required. Get a key at
https://console.groq.com/keys

The variable name `local_model` is kept for backward compatibility with
the three agent files that import it (research_agent.py, risk_agent.py,
trader_agent.py) - despite the name, this is now cloud-hosted, not local.

API key resolution order:
  1. GROQ_API_KEY in the environment (works for the CLI, loaded from a
     local .env file via python-dotenv)
  2. Streamlit secrets, bridged into the environment by dashboard.py
     before this module is imported (works when deployed on Streamlit
     Community Cloud)
"""

import os
from agents import OpenAIChatCompletionsModel, ModelSettings
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file (e.g. on Streamlit Cloud)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL_NAME = "qwen/qwen3.6-27b"  # not part of the gpt-oss/Harmony family -
# gpt-oss-120b has a known bug where its internal "commentary" channel
# tag leaks through as a fake tool name instead of being parsed correctly,
# especially as tool schemas get larger. Note: Groq lists this as a
# preview model, not "production" - worth knowing, but the alternative
# is a production model with a live bug, so this is the better trade.

if not GROQ_API_KEY:
    raise RuntimeError(
        "GROQ_API_KEY is not set. Get a free key at https://console.groq.com/keys, "
        "then either:\n"
        "  - Local/CLI: add GROQ_API_KEY=your-key-here to a .env file in the project root\n"
        "  - Streamlit Cloud: add it under your app's Settings -> Secrets"
    )

groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
    max_retries=6,  # auto-retries on Groq's free-tier rate limits, honoring
    # the exact wait time Groq's error response specifies, instead of the
    # pipeline just failing on a transient 429. Note: this only helps for
    # limits you can wait out - it does NOT help if a single request is
    # simply too large for the per-minute cap, which is what the
    # reasoning_effort/max_tokens settings below are for.
)

local_model = OpenAIChatCompletionsModel(
    model=GROQ_MODEL_NAME,
    openai_client=groq_client,
)

# Passed separately to each Agent(...) call (model_settings belongs on the
# Agent, not the Model object) - see research_agent.py / risk_agent.py /
# trader_agent.py, each of which does: model_settings=local_model_settings
local_model_settings = ModelSettings(
    # qwen3.6-27b defaults to "thinking mode" - it generates a large
    # internal reasoning trace before its actual answer, and those
    # reasoning tokens count against Groq's strict output-token-per-
    # minute limit (1,000 OTPM on the free tier). A single response
    # was hitting 1,295 output tokens - over the limit in ONE request,
    # which retrying can't fix. reasoning_effort="none" turns off that
    # internal reasoning trace, since this project needs a direct
    # analytical answer, not step-by-step math/coding reasoning.
    extra_body={"reasoning_effort": "none"},
    # Second layer of protection: hard-cap how much any single
    # response can generate, so even a verbose answer can't blow
    # through the per-minute output limit on its own.
    max_tokens=800,
)     
 
