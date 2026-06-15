"""Vault-backed retrieval memory — recall instead of re-read.

The budget play: agents normally carry the whole growing transcript and re-send it on every turn,
so token cost scales with conversation length — the fastest way for a capped-tier paying user (or a
free one) to blow a context window. Instead, the session already persists to an Obsidian vault
(see vault.py); this module RETRIEVES only the few relevant notes for the current turn. Long memory
on a small context window: reference-don't-requote, the same principle as the protocol handoff, but
across sessions and addressable by relevance.

Retrieval is lexical (TF-IDF over note chunks) on purpose. Embeddings would mean an API call / token
spend per turn, which defeats the goal; words-in-common is free, offline, and good enough to surface
the right notes from a vault you wrote yourself. No new dependencies.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

# Tiny stoplist — drop the words that carry no retrieval signal so scoring isn't dominated by them.
_STOP = {
    "the", "a", "an", "and", "or", "but", "if", "of", "to", "in", "on", "for", "is", "are", "be",
    "this", "that", "it", "as", "at", "by", "with", "from", "so", "we", "you", "i", "not", "no",
    "do", "does", "can", "will", "would", "should", "its", "their", "they", "them", "than", "then",
    "into", "out", "up", "down", "over", "per", "via", "each", "any", "all", "one", "two", "three",
}
_TOKEN = re.compile(r"[a-zA-Z0-9_]+")


def _tokens(text: str) -> List[str]:
    return [t for t in (w.lower() for w in _TOKEN.findall(text))
            if len(t) > 1 and t not in _STOP]


def est_tokens(text: str) -> int:
    """Rough token count (~4 chars/token) — for relative before/after comparison only."""
    return max(1, len(text) // 4)


def _chunk(name: str, text: str) -> List[Tuple[str, str]]:
    """Split a note into paragraph-sized chunks so recall returns a relevant slice, not a whole file."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return [(name, b) for b in blocks if len(b) > 12]


class VaultMemory:
    """Lexical (TF-IDF) recall over every markdown note in an Obsidian vault.

    recall(query) returns a compact, source-tagged block of the most relevant chunks — drop that in
    instead of re-sending the whole history. Built once per session; cheap to query.
    """

    def __init__(self, vault_dir) -> None:
        self.vault = Path(vault_dir)
        self.chunks: List[Tuple[str, str]] = []          # (source note, text)
        self._vecs: List[Counter] = []                   # token counts per chunk
        self._idf: Dict[str, float] = {}
        self._full_chars = 0                             # total size of the vault (for savings math)
        self._load()

    # ------------------------------------------------------------------ index
    def _load(self) -> None:
        if not self.vault.is_dir():
            return
        for fp in sorted(self.vault.rglob("*.md")):
            try:
                text = fp.read_text(encoding="utf-8")
            except OSError:
                continue
            self._full_chars += len(text)
            self.chunks.extend(_chunk(fp.stem, text))
        self._vecs = [Counter(_tokens(t)) for _, t in self.chunks]
        df: Counter = Counter()
        for v in self._vecs:
            df.update(v.keys())
        n = max(1, len(self.chunks))
        self._idf = {term: math.log(1 + n / c) for term, c in df.items()}

    @property
    def ready(self) -> bool:
        return bool(self.chunks)

    def stats(self) -> Dict[str, int]:
        notes = len({s for s, _ in self.chunks})
        return {"notes": notes, "chunks": len(self.chunks),
                "vault_tokens": est_tokens("x" * self._full_chars)}

    # ----------------------------------------------------------------- recall
    def _score(self, qvec: Counter, cvec: Counter) -> float:
        return sum(tf * self._idf.get(term, 0.0) for term, _ in qvec.items()
                   for tf in (cvec.get(term, 0),) if tf)

    def rank(self, query: str, k: int = 5) -> List[Tuple[float, str, str]]:
        """Top-k (score, source, text) chunks for the query, best first, score > 0 only."""
        qvec = Counter(_tokens(query))
        if not qvec or not self.chunks:
            return []
        scored = [(self._score(qvec, self._vecs[i]), self.chunks[i][0], self.chunks[i][1])
                  for i in range(len(self.chunks))]
        scored = [s for s in scored if s[0] > 0]
        scored.sort(key=lambda s: s[0], reverse=True)
        return scored[:k]

    def recall(self, query: str, k: int = 5, max_chars: int = 1800) -> str:
        """A compact, source-tagged memory block to inject in place of re-sending history.

        Returns "" when nothing is relevant — callers should add nothing rather than a header for
        an empty recall (so an off-topic turn pays zero memory tokens).
        """
        hits = self.rank(query, k)
        if not hits:
            return ""
        lines = ["## Relevant memory (recalled from the vault — not the full history)"]
        used = len(lines[0])
        for _, source, text in hits:
            snippet = " ".join(text.split())
            entry = f"- [[{source}]]: {snippet}"
            if used + len(entry) > max_chars and len(lines) > 1:
                break
            if len(entry) > max_chars:                    # a single huge chunk: trim to budget
                entry = entry[:max_chars].rsplit(" ", 1)[0] + " …"
            lines.append(entry)
            used += len(entry)
        return "\n".join(lines)

    def savings(self, query: str, k: int = 5, max_chars: int = 1800) -> Dict[str, int]:
        """Tokens to recall the relevant slice vs to dump the whole vault — the budget headline."""
        recalled = self.recall(query, k, max_chars)
        full_tok = est_tokens("x" * self._full_chars)
        rec_tok = est_tokens(recalled)
        pct = int(100 * (full_tok - rec_tok) / full_tok) if full_tok else 0
        return {"full_tokens": full_tok, "recall_tokens": rec_tok, "saved_pct": pct}
