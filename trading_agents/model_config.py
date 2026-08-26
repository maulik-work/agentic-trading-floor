"""
Model config - uses Groq's free, hosted, OpenAI-compatible API instead
of a local Ollama instance. Switched from Ollama specifically to enable
public deployment (Streamlit Community Cloud can't run Ollama for you,
since there's no local machine to host the model on).

Free tier: no credit card required, ~30 requests/minute. Get a key at
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
from agents import OpenAIChatCompletionsModel
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv()  # no-op if there's no .env file (e.g. on Streamlit Cloud)

GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL_NAME = "openai/gpt-oss-120b"

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
    max_retries=6,  # auto-retries on Groq's free-tier rate limits (8K TPM),
    # honoring the exact wait time Groq's error response specifies, instead
    # of the pipeline just failing on a transient 429.
)
local_model = OpenAIChatCompletionsModel(
    model=GROQ_MODEL_NAME,
    openai_client=groq_client,
)
