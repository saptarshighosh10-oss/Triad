"""Interactive API-key setup: masked prompt -> live validation -> safe save.

    python -m triad setup              # core providers (OpenAI / Anthropic / Gemini)
    python -m triad setup --all        # also free-cloud providers (NIM / Groq / OpenRouter)
    python -m triad setup --no-validate
    python -m triad setup --reconfigure

Validation hits each provider's `GET /models` endpoint, which confirms the key
works without spending any tokens. A bad key is caught before it's saved.
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from getpass import getpass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from . import dotenv, keychain

# Validation result: True = valid, False = rejected, None = couldn't verify (e.g. offline).
Validation = Tuple[Optional[bool], str]
_REJECT = {400, 401, 403}


def _check(url: str, headers: Dict[str, str], timeout: float = 10.0) -> Validation:
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, f"valid ({r.status})"
    except urllib.error.HTTPError as e:
        if e.code in _REJECT:
            return False, f"key rejected ({e.code})"
        if e.code == 429:
            return True, "valid (rate-limited)"
        return None, f"unverified (HTTP {e.code})"
    except Exception as e:  # DNS / timeout / offline
        return None, f"unverified ({type(e).__name__})"


def _v_openai(k: str) -> Validation:
    return _check("https://api.openai.com/v1/models", {"Authorization": f"Bearer {k}"})


def _v_anthropic(k: str) -> Validation:
    return _check("https://api.anthropic.com/v1/models",
                  {"x-api-key": k, "anthropic-version": "2023-06-01"})


def _v_gemini(k: str) -> Validation:
    return _check("https://generativelanguage.googleapis.com/v1beta/models",
                  {"x-goog-api-key": k})


def _v_nim(k: str) -> Validation:
    return _check("https://integrate.api.nvidia.com/v1/models", {"Authorization": f"Bearer {k}"})


def _v_groq(k: str) -> Validation:
    return _check("https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {k}"})


def _v_openrouter(k: str) -> Validation:
    return _check("https://openrouter.ai/api/v1/models", {"Authorization": f"Bearer {k}"})


class Provider:
    def __init__(self, name: str, env: str, signup: str, validate: Callable[[str], Validation]):
        self.name = name
        self.env = env
        self.signup = signup
        self.validate = validate


CORE: List[Provider] = [
    Provider("OpenAI (ChatGPT)", "OPENAI_API_KEY",
             "https://platform.openai.com/api-keys", _v_openai),
    Provider("Anthropic (Claude)", "ANTHROPIC_API_KEY",
             "https://console.anthropic.com/settings/keys", _v_anthropic),
    Provider("Google (Gemini)", "GEMINI_API_KEY",
             "https://aistudio.google.com/apikey", _v_gemini),
]

# Free-cloud providers (for routing free-claude-code or other gateways).
EXTRA: List[Provider] = [
    Provider("NVIDIA NIM", "NVIDIA_NIM_API_KEY",
             "https://build.nvidia.com/settings/api-keys", _v_nim),
    Provider("Groq", "GROQ_API_KEY", "https://console.groq.com/keys", _v_groq),
    Provider("OpenRouter", "OPENROUTER_API_KEY", "https://openrouter.ai/keys", _v_openrouter),
]


def mask(key: str) -> str:
    if not key:
        return "(not set)"
    if len(key) <= 8:
        return "•" * len(key)
    return f"{key[:3]}…{key[-4:]}"


def _prompt_key(prompt: str) -> str:
    try:
        return getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception:
        # Fallback for terminals where getpass can't hide input.
        return input(prompt).strip()


def run_setup(env_path, providers: List[Provider], validate: bool = True,
              reconfigure: bool = False) -> None:
    env_path = Path(env_path)
    existing = dotenv.parse(env_path)

    use_keychain = False
    if keychain.available():
        ans = input("Store keys in your OS keychain instead of a plaintext .env? [y/N] ").strip().lower()
        use_keychain = ans in ("y", "yes")

    print()
    saved = 0
    for p in providers:
        current = existing.get(p.env) or os.environ.get(p.env, "") or (
            keychain.get_key(p.env) if use_keychain else "")

        if current:
            print(f"{p.name}: currently {mask(current)}")
            entry = _prompt_key("  paste a new key, or press Enter to keep: ") if not reconfigure \
                else _prompt_key("  paste key (Enter to skip): ")
        else:
            print(f"{p.name}: {mask('')}  —  get one at {p.signup}")
            entry = _prompt_key("  paste key (Enter to skip): ")

        if not entry:
            print("  kept.\n" if current and not reconfigure else "  skipped.\n")
            continue

        if validate:
            ok, detail = p.validate(entry)
            symbol = {True: "✓", False: "✗", None: "?"}[ok]
            print(f"  {symbol} {detail}")
            if ok is False:
                if input("  save anyway? [y/N] ").strip().lower() not in ("y", "yes"):
                    print("  not saved.\n")
                    continue

        if use_keychain:
            keychain.set_key(p.env, entry)
        else:
            dotenv.write_var(env_path, p.env, entry)
        os.environ[p.env] = entry  # live for the current process too
        saved += 1
        print("  saved.\n")

    if not use_keychain and env_path.exists():
        if dotenv.ensure_gitignore(env_path):
            print(f"Added {env_path.name} to .gitignore")

    where = "your OS keychain" if use_keychain else str(env_path)
    print(f"Done — {saved} key(s) saved to {where}.")
