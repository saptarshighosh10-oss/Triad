#!/usr/bin/env python3
"""Live gate for the free roster — the one check that turns "construction passes" into
"it works". Makes ONE tiny real streaming call per free provider through triad's actual
agent classes, isolates each (one provider's failure can't mask another), and prints the
real model + error so provider-specific quirks (NIM extra_body, Groq 400s, OpenRouter
limits, SSE framing) surface precisely.

    python tools/smoke_free.py            # tests whichever free keys are present
    python tools/smoke_free.py --all      # also tests paid + the fcc-Claude slot

Keys come from the environment / .env (run `python -m triad setup --all` first). Nothing is
printed that reveals a key. This is a GATE: don't build Stage 2 until this is green.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# Load .env before importing config-bound modules (same discipline as __main__).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from triad import dotenv  # noqa: E402
dotenv.load(Path(__file__).resolve().parent.parent / ".env")

from triad.agents import (  # noqa: E402
    GroqAgent, OpenRouterAgent, NIMAgent, OpenAIAgent, ClaudeAgent, GeminiAgent,
)

PROMPT = "Reply with exactly the two characters: ok"


async def probe(agent) -> tuple[bool, str]:
    """One minimal round-trip through the real streaming path. Returns (ok, detail)."""
    os.environ.setdefault("TRIAD_MAX_TOKENS", "16")  # keep it tiny; free anyway
    try:
        out = await asyncio.wait_for(
            agent.complete_raw([{"role": "user", "content": PROMPT}]), timeout=60)
        out = out.strip()
        if not out:
            return False, "empty response (streamed 0 chars — check SSE handling)"
        return True, f'got {len(out)} chars: {out[:40]!r}'
    except Exception as e:  # provider param rejection, auth, timeout, SSE framing, …
        return False, f"{type(e).__name__}: {str(e)[:160]}"


async def main() -> int:
    ap = argparse.ArgumentParser(description="Live smoke test for triad's free roster.")
    ap.add_argument("--all", action="store_true", help="also probe paid agents + fcc-Claude slot")
    args = ap.parse_args()

    candidates = [GroqAgent(), OpenRouterAgent(), NIMAgent()]
    if args.all:
        candidates += [OpenAIAgent(), ClaudeAgent(), GeminiAgent()]

    print("triad free-roster smoke test\n" + "=" * 48)
    tested = passed = 0
    for agent in candidates:
        if not agent.available:
            print(f"  –  {agent.label:11} skipped (no key / base_url)")
            continue
        tested += 1
        tag = "  (via fcc)" if (agent.name == "claude" and agent.base_url) else ""
        ok, detail = await probe(agent)
        passed += ok
        mark = "✓" if ok else "✗"
        print(f"  {mark}  {agent.label:11} [{agent.model}]{tag}\n        {detail}")

    print("=" * 48)
    print(f"{passed}/{tested} providers responded." if tested else "No keys present — nothing to test.")
    if tested and passed < tested:
        print("GATE: not green — fix the failing provider(s) before building on the free roster.")
    elif tested:
        print("GATE: green — the free substrate actually streams. Safe to build Stage 2 on it.")
    return 0 if tested and passed == tested else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
