"""Central configuration — resolved LAZILY from the environment.

The earlier version snapshotted os.environ into a module-level dict at import time, so
any `TRIAD_*` override that arrived later (from `.env`, or set after import) was silently
ignored — and the bug came back the moment anything imported config before the .env load.
Reading the environment at agent-construction time instead makes import order irrelevant.

Model IDs change constantly and free catalogs drift fast, so everything here is
env-overridable; validate any free slug before trusting it. `base_url` (optional) points an
agent at an OpenAI- or Anthropic-compatible gateway instead of the provider's own endpoint —
that's the seam for free/local models.
"""
import os

# Static spec only: which env vars to read + the fallback default. No env reads here —
# resolve() does them on demand. (env var, default) resolved at construction.
_SPECS = {
    # ---- paid frontier roster ----
    "chatgpt": {"key_env": "OPENAI_API_KEY",
                "model_env": "TRIAD_OPENAI_MODEL", "model_default": "gpt-4.1"},
    "claude":  {"key_env": "ANTHROPIC_API_KEY",
                "model_env": "TRIAD_CLAUDE_MODEL", "model_default": "claude-sonnet-4-6",
                # Set TRIAD_CLAUDE_BASE_URL to free-claude-code (e.g. http://localhost:8082) to run
                # the Claude slot on a FREE model — fcc speaks the Anthropic API, so nothing else
                # changes. fcc routes by the model's *tier*, so TRIAD_CLAUDE_MODEL just selects the
                # tier (claude-sonnet-4-6 -> fcc's MODEL_SONNET); the free model is set in fcc.
                "base_url_env": "TRIAD_CLAUDE_BASE_URL"},  # no default -> paid Anthropic unless set
    "gemini":  {"key_env": "GEMINI_API_KEY",
                "model_env": "TRIAD_GEMINI_MODEL", "model_default": "gemini-2.5-pro"},

    # ---- free-cloud roster (all OpenAI-compatible; differ only by base_url + model) ----
    # Defaults span THREE model lineages on purpose — Llama / GPT-OSS / Qwen. Three finetunes
    # of one base make correlated errors and hollow out aggregation, so keep lineages distinct.
    "groq":       {"key_env": "GROQ_API_KEY",       # Llama lineage
                   "model_env": "TRIAD_GROQ_MODEL", "model_default": "llama-3.3-70b-versatile",
                   "base_url_env": "TRIAD_GROQ_BASE_URL",
                   "base_url_default": "https://api.groq.com/openai/v1"},
    "openrouter": {"key_env": "OPENROUTER_API_KEY",  # GPT-OSS lineage (free; catalog drifts — verify)
                   "model_env": "TRIAD_OPENROUTER_MODEL",
                   "model_default": "openai/gpt-oss-120b:free",
                   "base_url_env": "TRIAD_OPENROUTER_BASE_URL",
                   "base_url_default": "https://openrouter.ai/api/v1"},
    "nim":        {"key_env": "NVIDIA_NIM_API_KEY",  # Qwen lineage
                   "model_env": "TRIAD_NIM_MODEL", "model_default": "qwen/qwen2.5-coder-32b-instruct",
                   "base_url_env": "TRIAD_NIM_BASE_URL",
                   "base_url_default": "https://integrate.api.nvidia.com/v1"},
}

AGENTS = tuple(_SPECS)  # all configurable agent names, in order


def resolve(name: str) -> dict:
    """Resolve one agent's config from the CURRENT environment.

    Called at agent construction (not import), so `.env` and exported overrides always win
    regardless of import order. Returns {"model", "key_env", "base_url"}.
    """
    s = _SPECS[name]
    base_url = None
    if "base_url_env" in s:
        base_url = os.environ.get(s["base_url_env"], s.get("base_url_default"))
    return {
        "model": os.environ.get(s["model_env"], s["model_default"]),
        "key_env": s["key_env"],
        "base_url": base_url,
    }


def max_tokens() -> int:
    """Max output tokens per turn (Anthropic requires it; others honor it). Read lazily."""
    return int(os.environ.get("TRIAD_MAX_TOKENS", "4096"))
