"""Optional OS keyring storage (macOS Keychain, Windows Credential Locker, etc.)
via the `keyring` package. Every function degrades gracefully when `keyring`
isn't installed, so importing this module is always safe.

    pip install keyring   # to enable
"""
from __future__ import annotations

import os
from typing import Iterable, Optional

_SERVICE = "triad"


def available() -> bool:
    try:
        import keyring  # noqa: F401
        return True
    except Exception:
        return False


def set_key(env: str, value: str) -> bool:
    try:
        import keyring
        keyring.set_password(_SERVICE, env, value)
        return True
    except Exception:
        return False


def get_key(env: str) -> Optional[str]:
    try:
        import keyring
        return keyring.get_password(_SERVICE, env)
    except Exception:
        return None


def load_missing(env_names: Iterable[str]) -> int:
    """Fill os.environ from the keyring for any names not already set."""
    n = 0
    for name in env_names:
        if not os.environ.get(name):
            v = get_key(name)
            if v:
                os.environ[name] = v
                n += 1
    return n
