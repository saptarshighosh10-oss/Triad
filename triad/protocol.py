"""Compact inter-agent handoff protocol.

The point: in relay/council, agents currently re-send the whole growing transcript
on every hop. That's the real token waste. This module gives a terse, in-distribution
(so capability holds) and human-readable (so you keep oversight) handoff format, plus a
reference store so full outputs live somewhere addressable while only compact digests
get passed forward.

Format agents are asked to emit:

    @goal <one line>
    @find
    - <terse point>
    - <terse point>
    @conf <0.0-1.0>
    @next <handoff note / 'done'>
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

PROTOCOL_INSTRUCTION = (
    "Respond in this compact handoff format, nothing else:\n"
    "@goal <one line: what you're solving>\n"
    "@find\n"
    "- <terse finding or contribution>\n"
    "- <terse finding>\n"
    "@conf <your confidence, 0.0-1.0>\n"
    "@next <handoff: what's open / who should act / 'done'>\n"
    "Be dense. No preamble, no restating the question."
)


def est_tokens(text: str) -> int:
    """Rough token count (~4 chars/token). Fine for relative before/after comparison."""
    return max(1, len(text) // 4)


@dataclass
class Note:
    raw: str
    goal: str = ""
    find: List[str] = field(default_factory=list)
    conf: str = ""
    nxt: str = ""


def parse_note(text: str) -> Note:
    note = Note(raw=text)
    section = None
    for line in text.splitlines():
        s = line.strip()
        low = s.lower()
        if low.startswith("@goal"):
            note.goal = s[5:].strip(": ").strip()
            section = None
        elif low.startswith("@find"):
            section = "find"
        elif low.startswith("@conf"):
            note.conf = s[5:].strip(": ").strip()
            section = None
        elif low.startswith("@next"):
            note.nxt = s[5:].strip(": ").strip()
            section = None
        elif section == "find" and s.startswith(("-", "*", "•")):
            note.find.append(s.lstrip("-*• ").strip())
    return note


def compact_block(text: str, label: str, max_chars: int = 700) -> str:
    """A short, forward-passable digest of one agent's output.

    If the agent obeyed the protocol, re-emit the essential lines. Otherwise fall back
    to a truncated digest so an off-format reply still doesn't blow up the next prompt.
    """
    note = parse_note(text)
    if note.find or note.conf or note.nxt:
        parts = [f"## {label}"]
        if note.find:
            parts.append("@find")
            parts.extend(f"- {f}" for f in note.find)
        if note.conf:
            parts.append(f"@conf {note.conf}")
        if note.nxt:
            parts.append(f"@next {note.nxt}")
        return "\n".join(parts)
    # fallback: trim raw
    trimmed = text.strip()
    if len(trimmed) > max_chars:
        trimmed = trimmed[:max_chars].rsplit(" ", 1)[0] + " …"
    return f"## {label}\n{trimmed}"


class RefStore:
    """Holds full outputs under addressable keys so handoffs can pass a pointer
    (ref:key) and the human transcript can still recover everything."""

    def __init__(self) -> None:
        self._d: Dict[str, str] = {}

    def put(self, key: str, value: str) -> str:
        self._d[key] = value
        return f"ref:{key}"

    def get(self, key: str) -> str:
        return self._d.get(key, "")

    def clear(self) -> None:
        self._d.clear()
