"""Persistent rate-limit cooldowns for the free roster, shared across one-shot `ask` runs.

Each `triad-ask` is a fresh process, so a 429 seen on one call would be forgotten by the next
without this. We record *when* a model becomes usable again; the selector then skips a cooling
model and — crucially — re-promotes the better model automatically once its cooldown passes.

Plain stdlib JSON at ~/.config/triad/cooldowns.json. Best-effort: any IO error degrades to
"nothing is cooling" rather than breaking a call.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

STORE = Path.home() / ".config" / "triad" / "cooldowns.json"


def _load() -> dict:
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    try:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        STORE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


def available_at(spec: str) -> float:
    """Epoch seconds when `spec` is usable again (0 if never marked)."""
    try:
        return float(_load().get(spec, 0.0))
    except (TypeError, ValueError):
        return 0.0


def seconds_left(spec: str, now: float | None = None) -> float:
    """How long until `spec` is usable again — 0 if it's ready now."""
    return max(0.0, available_at(spec) - (now if now is not None else time.time()))


def is_cooling(spec: str, now: float | None = None) -> bool:
    return seconds_left(spec, now) > 0.0


def mark(spec: str, seconds: float) -> None:
    """Mark `spec` rate-limited for `seconds` from now."""
    data = _load()
    data[spec] = time.time() + max(0.0, float(seconds))
    _save(data)


def clear(spec: str) -> None:
    """A model answered cleanly — forget any cooldown so it ranks normally again."""
    data = _load()
    if data.pop(spec, None) is not None:
        _save(data)
