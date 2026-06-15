"""Obsidian-compatible vault: persist a triad session as a living knowledge graph of
markdown notes with [[wikilinks]], so the folder opens straight into Obsidian's graph view.

Layout (open the folder itself as a vault):
    index.md              hub — links every agent + topic + checkpoint note
    agents/<name>.md      one living note per agent (role + a dated Log, appended each turn)
    topics/<topic>.md     seeded project concepts, cross-linked so the graph isn't empty
    checkpoints/<...>.md   session checkpoints

Wikilinks resolve by basename across the whole vault, so link text == filename (spaces are
fine). Everything here is plain files: Obsidian watches the folder and shows new notes live.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---- seeded project concepts (the graph spine). Bodies cross-link via [[wikilinks]]. ----
TOPICS: Dict[str, str] = {
    "free-cloud default": (
        "Default to **free cloud** models, not local. The target user has no budget and a "
        "MacBook Air, so 'just run it locally' is the privilege answer — free cloud routing is "
        "the point, local is an opt-in.\n\n"
        "Related: [[cross-provider decorrelation]], [[layer 2 free-claude-code]], [[generate-verify-select]]."
    ),
    "cross-provider decorrelation": (
        "Aggregating free models only helps if their errors are **independent**. Three vendors "
        "serving finetunes of one base (all Llama, say) make *correlated* errors and then agree — "
        "pseudo-diversity. Pick distinct lineages (Llama / GPT-OSS / Qwen).\n\n"
        "Related: [[free-cloud default]], [[generate-verify-select]]."
    ),
    "generate-verify-select": (
        "The Stage 2 primitive for verifiable work: generate N diverse candidates → run an "
        "**oracle** → keep the ones that pass → feed failures back to revise. Best-of-N against an "
        "executable check, not a discussion (synthesis would blend correlated-wrong answers).\n\n"
        "Related: [[oracle independence]], [[execution sandbox]], [[cross-provider decorrelation]]."
    ),
    "oracle independence": (
        "generate-verify-select only works if the verifier isn't captured by the generator. If the "
        "free models write both the code and its tests, they'll write tests that pass their own wrong "
        "code. The oracle must be independent: user tests, a separate test-author step, or ground-truth "
        "execution.\n\n"
        "Related: [[generate-verify-select]], [[execution sandbox]]."
    ),
    "execution sandbox": (
        "Stage 2 runs free-model-**generated** code, so 'never give cheap models something you can't "
        "`git reset`' isn't enough — you need a real sandbox (subprocess/container, no host FS, no "
        "network, time/memory limits). Never on the host.\n\n"
        "Related: [[generate-verify-select]], [[oracle independence]]."
    ),
    "layer 1 triad": (
        "This repo: a terminal orchestrator running ChatGPT/Claude/Gemini (or a free roster) in "
        "parallel / relay / council. Paid frontier APIs by default; chat-only, never edits files.\n\n"
        "Related: [[layer 2 free-claude-code]], [[layer 3 sidecar]], [[free-cloud default]]."
    ),
    "layer 2 free-claude-code": (
        "Separate tool: a proxy that drives a real file-editing coding agent on **free/local** models. "
        "Anthropic-shaped (`/v1/messages` on localhost:8082), so triad's Claude slot can run free "
        "through it via `TRIAD_CLAUDE_BASE_URL`.\n\n"
        "Related: [[layer 1 triad]], [[layer 3 sidecar]]."
    ),
    "layer 3 sidecar": (
        "Separate tool: cheap models as **advisory-only** reviewers — second opinions, no file edits, "
        "no final calls.\n\n"
        "Related: [[layer 1 triad]], [[layer 2 free-claude-code]]."
    ),
}

# Which topics each agent's living note links to (gives agent↔topic edges in the graph).
_AGENT_FOCUS: Dict[str, List[str]] = {
    "chatgpt": ["generate-verify-select", "execution sandbox"],
    "claude": ["oracle independence", "free-cloud default"],
    "gemini": ["cross-provider decorrelation", "layer 2 free-claude-code"],
    "groq": ["free-cloud default", "cross-provider decorrelation"],
    "openrouter": ["cross-provider decorrelation", "generate-verify-select"],
    "nim": ["free-cloud default", "execution sandbox"],
}
_DEFAULT_FOCUS = ["generate-verify-select", "free-cloud default"]

_LINK_RE = re.compile(r"\[\[([^\]|#]+)")  # capture link target, ignoring |alias and #heading


def _today() -> str:
    return datetime.date.today().isoformat()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text.rstrip() + "\n", encoding="utf-8")


def ensure_dirs(vault: Path) -> None:
    for sub in ("agents", "topics", "checkpoints"):
        (vault / sub).mkdir(parents=True, exist_ok=True)


def linkify(text: str) -> str:
    """Wrap the first mention of any known topic name in [[...]] so summaries form edges."""
    for name in sorted(TOPICS, key=len, reverse=True):  # longest first: avoid nested matches
        if f"[[{name}]]" in text:
            continue
        m = re.search(re.escape(name), text, re.IGNORECASE)
        if m:
            text = text[:m.start()] + f"[[{name}]]" + text[m.end():]
    return text


def _summarize(text: str, limit: int = 240) -> str:
    s = " ".join(text.split())
    if len(s) <= limit:
        return s
    return s[:limit].rsplit(" ", 1)[0] + "…"   # trim to a word boundary only when truncating


# ----------------------------------------------------------------- seeding
def seed_topics(vault: Path) -> int:
    """Write the concept notes (only if missing, so edits survive). Returns count written."""
    n = 0
    for name, body in TOPICS.items():
        p = vault / "topics" / f"{name}.md"
        if not p.exists():
            _write(p, f"# {name}\n\n{body}")
            n += 1
    return n


def seed_agent_note(vault: Path, name: str, label: str, model: str) -> None:
    """Create a living note for one agent if it doesn't exist yet."""
    p = vault / "agents" / f"{name}.md"
    if p.exists():
        return
    focus = _AGENT_FOCUS.get(name, _DEFAULT_FOCUS)
    links = ", ".join(f"[[{t}]]" for t in focus)
    body = (
        f"# {label}\n\n"
        f"Living note for the **{label}** agent (model: `{model}`). Part of the [[index|triad]] "
        f"roster — see [[layer 1 triad]]. Focus: {links}.\n\n"
        f"## Log\n"
        f"- {_today()} — joined the session; focus on {links}.\n"
    )
    _write(p, body)


def _list_notes(vault: Path, sub: str) -> List[str]:
    d = vault / sub
    return sorted(p.stem for p in d.glob("*.md")) if d.is_dir() else []


def seed_index(vault: Path) -> None:
    """(Re)write index.md, linking every agent, topic, and checkpoint that exists."""
    agents = _list_notes(vault, "agents")
    checkpoints = _list_notes(vault, "checkpoints")
    lines = [
        "# triad — vault index",
        "",
        "Living knowledge graph for this triad project. Open this folder as an Obsidian vault "
        "(*Open folder as vault*) and hit the graph view (Cmd/Ctrl+G).",
        "",
        "## Agents",
        *([f"- [[{a}]]" for a in agents] or ["- _(none yet — run `triad --vault <dir>`)_"]),
        "",
        "## Topics",
        *[f"- [[{t}]]" for t in TOPICS],
        "",
        "## Checkpoints",
        *([f"- [[{c}]]" for c in checkpoints] or ["- _(none yet)_"]),
        "",
        f"_Updated {_today()} · triad --vault_",
    ]
    _write(vault / "index.md", "\n".join(lines))


def open_vault(vault_dir: str, agents, skills=None) -> Path:
    """Create + seed the vault: topics, a living note per active agent, and the index."""
    vault = Path(vault_dir)
    ensure_dirs(vault)
    seed_topics(vault)
    for a in agents:
        seed_agent_note(vault, a.name, a.label, a.model)
    seed_index(vault)
    return vault


# ----------------------------------------------------------------- updating
def _last_turn_by_label(orch) -> Dict[str, str]:
    """Parse the most recent transcript block into {agent label: text} (works for every mode)."""
    transcript = getattr(orch, "transcript", None)
    if not transcript:
        return {}
    out: Dict[str, str] = {}
    cur: Optional[str] = None
    buf: List[str] = []
    for line in transcript[-1].splitlines():
        if line.startswith("### "):
            if cur is not None:
                out[cur] = "\n".join(buf).strip()
            cur, buf = line[4:].strip(), []
        elif line.startswith("## ["):
            continue
        elif cur is not None:
            buf.append(line)
    if cur is not None:
        out[cur] = "\n".join(buf).strip()
    return out


def remember(vault_dir: str, agents, orch) -> int:
    """Append each agent's latest contribution to its living note (linkified). Returns the
    number of agent notes updated. Safe to call every turn or from /remember."""
    vault = Path(vault_dir)
    ensure_dirs(vault)
    idx = len(getattr(orch, "transcript", []))
    if idx and getattr(orch, "_remembered_upto", 0) >= idx:
        return 0  # nothing new since the last write — don't duplicate the bullet
    by_label = _last_turn_by_label(orch)
    updated = 0
    for a in agents:
        seed_agent_note(vault, a.name, a.label, a.model)  # ensure note exists (e.g. /keys added it)
        # Latest turn from the transcript (covers every mode); fall back to history if needed.
        text = by_label.get(a.label) or (
            a.history[-1]["content"] if a.history and a.history[-1]["role"] == "assistant" else "")
        summary = _summarize(text)
        if not summary:
            continue
        bullet = f"- {_today()} ({orch.mode}) — {linkify(summary)}\n"
        with (vault / "agents" / f"{a.name}.md").open("a", encoding="utf-8") as fh:
            fh.write(bullet)
        updated += 1
    orch._remembered_upto = idx
    seed_index(vault)
    return updated


def archive_evicted(vault_dir: str, name: str, label: str, messages: List[Dict[str, str]]) -> int:
    """Summarize history exchanges trimmed from the live context into memory/<name>.md, so the detail
    stays *recallable* even though it left the context window. Returns the number of exchanges saved.

    This is what makes bounded history lossless against the vault: VaultMemory indexes memory/*.md, so
    a trimmed turn can be pulled back later by relevance instead of being gone."""
    vault = Path(vault_dir)
    (vault / "memory").mkdir(parents=True, exist_ok=True)
    p = vault / "memory" / f"{name}.md"
    if not p.exists():
        _write(p, f"# {label} — long-term memory\n\nSummaries of older turns trimmed from the live "
                  f"context, kept so recall can still find them. Part of [[layer 1 triad]].\n\n## Log")
    lines, pending_q = [], None
    for m in messages:
        if m.get("role") == "user":
            pending_q = _summarize(m.get("content", ""), 160)
        elif m.get("role") == "assistant":
            a = _summarize(m.get("content", ""), 220)
            if a:
                q = f"Q: {pending_q} → " if pending_q else ""
                lines.append(f"- {_today()} — {linkify(q + 'A: ' + a)}")
            pending_q = None
    if not lines:
        return 0
    with p.open("a", encoding="utf-8") as fh:
        fh.write("\n" + "\n".join(lines) + "\n")
    return len(lines)


def checkpoint(vault_dir: str, title: str, body: str) -> Path:
    """Write a checkpoint note (linkified) and relink it from the index. Returns its path."""
    vault = Path(vault_dir)
    ensure_dirs(vault)
    name = f"{_today()} {title}".strip()
    p = vault / "checkpoints" / f"{name}.md"
    _write(p, f"# {name}\n\n{linkify(body)}\n\nBack to [[index]].")
    seed_index(vault)
    return p


# ----------------------------------------------------------------- reading / checking
def resume_path(path: str) -> Path:
    """The file --seed-file should read: a vault dir resolves to its index.md."""
    p = Path(path)
    return p / "index.md" if p.is_dir() else p


def read_resume(path: str) -> str:
    """Read a vault's index.md (or a markdown file) back as resume context."""
    p = resume_path(path)
    return p.read_text(encoding="utf-8") if p.exists() else ""


def extract_links(text: str) -> List[str]:
    return [m.group(1).strip() for m in _LINK_RE.finditer(text)]


def check_links(vault_dir: str) -> Tuple[int, List[Tuple[str, str]]]:
    """Verify every [[wikilink]] resolves to a real note (by basename). Returns
    (total_links, orphans) where each orphan is (source_file, missing_target)."""
    vault = Path(vault_dir)
    names = {p.stem for p in vault.rglob("*.md")}
    total = 0
    orphans: List[Tuple[str, str]] = []
    for p in vault.rglob("*.md"):
        for target in extract_links(p.read_text(encoding="utf-8")):
            total += 1
            if target not in names:
                orphans.append((str(p.relative_to(vault)), target))
    return total, orphans
