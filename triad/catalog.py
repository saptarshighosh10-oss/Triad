"""Model catalog — ask each provider what it actually serves, validate your slugs, auto-fix drift.

Free model catalogs change constantly (a slug that worked last week 404s today). Instead of guessing,
this queries each OpenAI-compatible provider's /models endpoint, checks whether your configured
TRIAD_*_MODEL still exists, and (with --auto) picks a sensible valid default and writes it to .env.
That's "ask the models themselves which models are correct", done cheaply and deterministically —
no LLM call, just the provider's own catalog.

Claude/Gemini use different APIs (not OpenAI /models), so they're reported as 'not checked here'.
"""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import config
from .agents import Agent, OpenAICompatibleAgent, build_agents

# Per-provider preference order when auto-picking — keep lineages distinct (Llama / GPT-OSS / Qwen).
PREFS: Dict[str, List[str]] = {
    "chatgpt":    ["gpt-4.1", "gpt-4o", "gpt-4"],
    "groq":       ["llama-3.3-70b", "llama-3.1-70b", "70b", "llama"],
    "openrouter": ["gpt-oss-120b", "qwen", "llama", "free"],
    "nim":        ["qwen2.5-coder", "coder", "qwen", "nemotron", "llama"],
}


async def list_models(agent: Agent) -> List[str]:
    """Model ids the provider currently serves (OpenAI-compatible /models). [] on error/unsupported."""
    base = getattr(agent, "base_url", None)
    if not isinstance(agent, OpenAICompatibleAgent):
        return []
    try:
        from openai import AsyncOpenAI
    except ImportError:
        return []
    client = AsyncOpenAI(api_key=agent.api_key or "not-needed", base_url=base)
    try:
        res = await client.models.list()
        return sorted(m.id for m in res.data)
    except Exception:
        return []


def pick(name: str, current: str, available: List[str]) -> Optional[str]:
    """Keep `current` if the provider still serves it; else the first model matching a preference;
    else the first available. None when the catalog is empty."""
    if not available:
        return None
    if current in available:
        return current
    for kw in PREFS.get(name, []):
        for m in available:
            if kw.lower() in m.lower():
                return m
    return available[0]


def model_env(name: str) -> str:
    return config._SPECS[name]["model_env"]


def upsert_env(env_path: Path, key: str, value: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    pref = key + "="
    for i, ln in enumerate(lines):
        if ln.strip().startswith(pref):
            lines[i] = pref + value
            break
    else:
        lines.append(pref + value)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def audit(agents: List[Agent]) -> List[Tuple[str, str, bool, Optional[str], int]]:
    """Per agent: (name, current_model, current_is_valid, suggested, n_available)."""
    rows = []
    for a in agents:
        models = await list_models(a)
        cur = a.model
        valid = cur in models if models else False
        suggested = pick(a.name, cur, models)
        rows.append((a.name, cur, valid, suggested, len(models)))
    return rows
