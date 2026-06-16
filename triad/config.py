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


_CODE_SIGNALS = frozenset({
    "code", "implement", "write", "fix", "edit", "refactor", "function", "class",
    "bug", "test", "script", "debug", "build", "create", "def ", "```",
})
_MEDIUM_SIGNALS = frozenset({
    "explain", "why", "describe", "compare", "difference", "how does", "how do",
    "walk me through", "break down", "pros and cons", "trade", "vs ", "versus",
})
_SIMPLE_PREFIXES = ("what ", "who ", "when ", "where ", "is ", "does ", "can ", "list ", "name ")


# Free OpenRouter models ranked by capability — used as swarm workers in plan-execute.
# All use the :free suffix; catalog drifts so verify slugs if one stops working.
FREE_OR_MODELS = [
    "deepseek/deepseek-r1:free",
    "deepseek/deepseek-chat-v3-5:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "qwen/qwen2.5-72b-instruct:free",
    "microsoft/phi-4-reasoning:free",
    "qwen/qwen2.5-coder-32b-instruct:free",
    "google/gemma-2-27b-it:free",
    "mistralai/mistral-7b-instruct:free",
    "meta-llama/llama-3.1-8b-instruct:free",
    "google/gemma-2-9b-it:free",
]


def is_bad_output(text: str, tier: str) -> tuple:
    """Detect outputs worth retrying: too short, refusal, no code block, truncated."""
    t = text.strip()
    if len(t) < 40:
        return True, "too short"
    low = t.lower()
    if any(low.startswith(r) for r in ("i cannot", "i'm unable", "i am unable", "as an ai")):
        return True, "refusal"
    if tier == "code" and "```" not in t:
        return True, "no code block"
    if t and t[-1].isalpha() and "```" not in t[-30:]:
        return True, "truncated"
    return False, ""


def classify_task(task: str) -> dict:
    """Classify a task into a tier — $0, no API call, pure regex.

    Returns tier, max_tokens, budget_hint, cot_hint, format_hint.
    cot_hint and format_hint are injected into free-model prompts to improve output quality.
    """
    t = task.lower().strip()
    if any(sig in t for sig in _CODE_SIGNALS):
        return {
            "tier": "code", "max_tokens": 2048, "budget_hint": "≤2000 tokens",
            "cot_hint": "Think step by step before writing code.",
            "format_hint": "Output ONLY a fenced code block. Nothing outside it.",
        }
    if any(sig in t for sig in _MEDIUM_SIGNALS):
        return {
            "tier": "medium", "max_tokens": 512, "budget_hint": "≤400 tokens",
            "cot_hint": "Reason briefly before answering.",
            "format_hint": "",
        }
    if len(task) < 100 or any(t.startswith(p) for p in _SIMPLE_PREFIXES):
        return {
            "tier": "simple", "max_tokens": 256, "budget_hint": "≤150 tokens",
            "cot_hint": "", "format_hint": "",
        }
    return {
        "tier": "medium", "max_tokens": 512, "budget_hint": "≤400 tokens",
        "cot_hint": "Reason briefly before answering.",
        "format_hint": "",
    }


def max_tokens(tier: str = "medium") -> int:
    """Max output tokens per turn. Tier-aware: simple=256, medium=512, code=2048.

    TRIAD_MAX_TOKENS env override still applies (hard ceiling for all tiers).
    Was a flat 4096 for every call — this alone cuts cost 30-87% depending on task mix.
    """
    defaults = {"simple": 256, "medium": 512, "code": 2048}
    base = defaults.get(tier, 512)
    env = os.environ.get("TRIAD_MAX_TOKENS")
    return int(env) if env else base
