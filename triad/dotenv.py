"""Minimal .env handling — parse, load into os.environ, and safely write keys.

No third-party dependency. Tolerates both `KEY=value` and `export KEY="value"`
styles so a hand-written or wizard-written file both work.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Union

PathLike = Union[str, Path]


def _strip_quotes(v: str) -> str:
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse(path: PathLike) -> Dict[str, str]:
    p = Path(path)
    out: Dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        if s.startswith("export "):
            s = s[len("export "):].strip()
        k, v = s.split("=", 1)
        out[k.strip()] = _strip_quotes(v.strip())
    return out


def load(path: PathLike, override: bool = False) -> None:
    """Populate os.environ from a .env file (existing vars win unless override)."""
    for k, v in parse(path).items():
        if override or k not in os.environ:
            os.environ[k] = v


def write_var(path: PathLike, key: str, value: str) -> None:
    """Insert or update KEY=value in place, preserving all other lines. chmod 600."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    out, found = [], False
    for line in lines:
        s = line.strip()
        name = None
        if s and not s.startswith("#") and "=" in s:
            lhs = s.split("=", 1)[0].strip()
            name = lhs[len("export "):].strip() if lhs.startswith("export ") else lhs
        if name == key:
            out.append(f"{key}={value}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"{key}={value}")
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        os.chmod(p, 0o600)  # owner read/write only — keys aren't world-readable
    except OSError:
        pass


def ensure_gitignore(env_path: PathLike) -> bool:
    """Make sure the .env filename is gitignored beside it. Returns True if added."""
    env_p = Path(env_path)
    gi = env_p.parent / ".gitignore"
    name = env_p.name
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.exists() else []
    if any(line.strip() == name for line in existing):
        return False
    with gi.open("a", encoding="utf-8") as fh:
        if existing and existing[-1].strip() != "":
            fh.write("\n")
        fh.write(name + "\n")
    return True
