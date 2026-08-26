"""
Agentic Trading Floor - CLI
------------------------------
Command-line interface. Uses the shared pipeline in pipeline.py.

Usage:
    uv run app\\main.py AAPL
    uv run app\\main.py IONEXCHANG.NS
    uv run app\\main.py OLAELEC dad        # optional 2nd arg: profile name
"""

import asyncio
import sys
from pipeline import run_pipeline


def print_result(result: dict):
    symbol = result["symbol"]
    print(f"\n=== RESEARCH: {symbol} (profile: {result['profile_id']}) ===")
    print(result["research"])
    print(f"\n=== RISK CHECK ===")
    print(result["risk"])
    print(f"\n=== TRADER DECISION ===")
    print(result["trader"])
    print(f"\n=== PORTFOLIO ===")
    p = result["portfolio"]
    print(f"Cash: {p['cash']}")
    print(f"Positions: {p['positions']}")


def main():
    if len(sys.argv) < 2:
        print("Usage: uv run app\\main.py <SYMBOL> [profile-name]")
        sys.exit(1)

    symbol = sys.argv[1]
    profile_id = sys.argv[2] if len(sys.argv) > 2 else "default"
    result = asyncio.run(run_pipeline(symbol, profile_id=profile_id))
    print_result(result)


if __name__ == "__main__":
    main()
