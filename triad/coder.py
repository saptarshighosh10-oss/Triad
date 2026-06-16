"""Three-head verify-select file editing — the dragon's coding mode (Stage 3).

[[generate-verify-select]] applied to a real repo: each free head proposes COMPLETE rewrites
of the files it wants to change; every proposal is applied to an in-memory copy of the repo and
run against the user's oracle inside the [[execution sandbox]]; the first proposal that PASSES
wins. No passer -> the failing output is fed back, bounded rounds. No oracle -> proposals come
back UNVERIFIED (selection only), never auto-applied — same honesty rule as verify mode.

Why whole-file rewrites instead of a tool-use agent loop: weak free models are unreliable at
multi-step tool calling and at emitting context-exact diffs, but they can return a whole file in
one shot — trivial to parse, nothing to mis-apply. And the real repo is NEVER touched until you
accept: generation and verification run on in-memory file maps + the sandbox, so the blast radius
is zero until apply(). The diversity of three distinct free lineages is what makes selection mean
something ([[cross-provider decorrelation]]); the oracle is what makes it trustworthy.
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from rich.console import Console

from .agents import Agent
from .memory import _tokens, est_tokens          # reuse the lexical tokenizer + token estimate
from .oracle import AbsentOracle, Oracle, Verdict
from .sandbox import Sandbox

# Directories never read into context (junk, build output, vendored deps, VCS internals).
_IGNORE_DIRS = {".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "env", "node_modules",
                ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".idea", ".vscode",
                "dist", "build", "__pypackages__", ".tox", "site-packages"}
_MAX_FILE_BYTES = 64 * 1024          # skip files larger than this when reading the repo
_MAX_CONTEXT_BYTES = 160 * 1024      # cap the repo context handed to each model


# --------------------------------------------------------------------- repo I/O
def read_repo(root, max_file_bytes: int = _MAX_FILE_BYTES) -> Dict[str, str]:
    """Map relpath -> text content for the repo's source files.

    Skips ignored dirs, dotfiles/dotdirs (which also keeps `.env` and other secrets out of the
    prompt), binaries, and oversized files. v1 is built for small repos / scratch tasks.
    """
    root = Path(root)
    files: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):           # skip dotfiles (.env, .DS_Store, …)
                continue
            fp = Path(dirpath) / fn
            try:
                if fp.stat().st_size > max_file_bytes:
                    continue
                raw = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw:               # crude but effective binary guard
                continue
            try:
                files[str(fp.relative_to(root))] = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
    return files


def _file_block(path: str, body: str) -> str:
    return f"FILE: {path}\n```\n{body}\n```\n"


def _relevance(qtokens: Counter, path: str, body: str) -> float:
    """Lexical overlap of a file with the task — same recall principle as the vault memory, applied
    to repo context: spend tokens on the files the task is actually about, not the whole tree."""
    fv = Counter(_tokens(path + "\n" + body))
    return sum(qtokens[t] * min(fv.get(t, 0), 4) for t in qtokens)


def build_context(task: str, files: Dict[str, str], max_bytes: int) -> Tuple[str, List[str]]:
    """Repo context for the prompt. If the whole repo fits the budget, include it all. Otherwise send
    the most task-relevant files IN FULL (greedy to budget) and list the rest by name only — the model
    still sees the full file map, just not every byte. Returns (context_text, paths_included_in_full)."""
    if not files:
        return "(the repository is currently empty)", []
    full = "\n".join(_file_block(p, files[p]) for p in sorted(files))
    if len(full) <= max_bytes:
        return full, list(files)

    qtokens = Counter(_tokens(task))
    tl = task.lower()
    scored: List[Tuple[float, str]] = []
    for path in files:
        score = _relevance(qtokens, path, files[path])
        if path.lower() in tl or Path(path).stem.lower() in tl:   # task names the file -> always include
            score += 1e6
        scored.append((score, path))
    scored.sort(key=lambda s: (-s[0], s[1]))

    parts, included, used = [], [], 0
    for _, path in scored:
        chunk = _file_block(path, files[path])
        if used + len(chunk) > max_bytes and included:
            break
        parts.append(chunk)
        used += len(chunk)
        included.append(path)
    omitted = [p for _, p in scored if p not in set(included)]
    if omitted:
        parts.append("OTHER FILES IN THE REPO (names only — say which you need and I'll resend):\n"
                     + "\n".join(f"- {p}" for p in sorted(omitted)))
    return "\n".join(parts), included


# ------------------------------------------------------------------ edit format (whole-file)
_EDIT_INSTRUCTION = (
    "Apply the change by returning the COMPLETE new contents of every file you create or modify.\n"
    "For EACH such file, output EXACTLY this and nothing around it:\n\n"
    "FILE: <relative/path>\n"
    "```\n"
    "<the entire new file content>\n"
    "```\n\n"
    "Rules: emit the WHOLE file, never a fragment or a diff. Include only files you change. "
    "Do not add explanations. Do not wrap the whole answer in a single block."
)

# FILE: <path> on its own line, then a fenced block. Tolerant of a language tag after the fence.
_EDIT_RE = re.compile(
    r"^FILE:[ \t]*(?P<path>.+?)[ \t]*\r?\n```[^\n]*\r?\n(?P<body>.*?)\r?\n?```",
    re.DOTALL | re.MULTILINE,
)

# ------------------------------------------------------------------ edit format (section-replace)
# Token-efficient alternative: model only emits changed hunks, not whole files.
# Saves 60-80% on output tokens for small edits to large files.
# Use with strong models (paid); free models are unreliable at context-exact matching.
_SECTION_EDIT_INSTRUCTION = (
    "Apply the change using ONLY section replacements — emit the changed hunks, not whole files.\n"
    "For EACH change, output EXACTLY this block:\n\n"
    "EDIT: <relative/path>\n"
    "FIND:\n"
    "<exact lines to replace — must match the file verbatim>\n"
    "END_FIND\n"
    "REPLACE:\n"
    "<new lines>\n"
    "END_REPLACE\n\n"
    "Rules: FIND must be a verbatim excerpt from the current file (whitespace exact). "
    "Multiple EDIT blocks allowed for multiple files. No explanations outside the blocks."
)

_SECTION_RE = re.compile(
    r"^EDIT:[ \t]*(?P<path>.+?)[ \t]*\r?\n"
    r"FIND:\r?\n(?P<find>.*?)END_FIND\r?\n"
    r"REPLACE:\r?\n(?P<replace>.*?)END_REPLACE",
    re.DOTALL | re.MULTILINE,
)


def parse_section_edits(text: str, base: Dict[str, str]) -> Dict[str, str]:
    """Apply FIND/REPLACE hunks to base file map, return updated file map.

    Only files that changed are included in the result (like parse_edits).
    If FIND text is not found verbatim, that hunk is silently skipped — caller
    checks via oracle whether the result is correct.
    """
    result: Dict[str, str] = {}
    for m in _SECTION_RE.finditer(text or ""):
        rel = _safe_relpath(m.group("path"))
        if rel is None:
            continue
        find_text = m.group("find")
        replace_text = m.group("replace")
        current = result.get(rel, base.get(rel, ""))
        if find_text in current:
            result[rel] = current.replace(find_text, replace_text, 1)
    return result


def _safe_relpath(path: str) -> Optional[str]:
    """Reject absolute paths and any `..` escape — model output is untrusted and may be written."""
    path = path.strip().strip("`").strip().strip('"').strip("'")
    while path.startswith("./"):
        path = path[2:]
    if not path or path.startswith(("/", "~")):
        return None
    parts = Path(path).parts
    if ".." in parts or (parts and parts[0] == ""):
        return None
    return path


def parse_edits(text: str) -> Dict[str, str]:
    """Pull {relpath: new_content} from a model reply in the FILE: + fenced-block format."""
    edits: Dict[str, str] = {}
    for m in _EDIT_RE.finditer(text or ""):
        rel = _safe_relpath(m.group("path"))
        if rel is not None:
            edits[rel] = m.group("body")
    return edits


def build_diff(base: Dict[str, str], edited: Dict[str, str]) -> str:
    """Unified diff across every path that changed between two file maps."""
    out: List[str] = []
    for path in sorted(set(base) | set(edited)):
        a, b = base.get(path), edited.get(path)
        if a == b:
            continue
        out.extend(difflib.unified_diff(
            (a or "").splitlines(keepends=True), (b or "").splitlines(keepends=True),
            fromfile=("a/" + path if a is not None else "/dev/null"),
            tofile=("b/" + path if b is not None else "/dev/null"),
        ))
        if out and not out[-1].endswith("\n"):
            out.append("\n")
    return "".join(out)


def apply_edits(root, edits: Dict[str, str]) -> List[str]:
    """Write the edits to disk (only on user accept). Re-guards paths defensively."""
    root = Path(root)
    written: List[str] = []
    for path, body in edits.items():
        rel = _safe_relpath(path)
        if rel is None:
            continue
        fp = root / rel
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body, encoding="utf-8")
        written.append(rel)
    return written


def _gen_prompt(task: str, files: Dict[str, str], max_ctx: int, fail: Optional[str] = None,
                diff_mode: bool = False) -> str:
    context, _ = build_context(task, files, max_ctx)
    instr = _SECTION_EDIT_INSTRUCTION if diff_mode else _EDIT_INSTRUCTION
    fmt = "EDIT: / FIND: / REPLACE: section format" if diff_mode else "FILE: format"
    p = (f"You are editing a code repository. TASK:\n{task}\n\n"
         f"CURRENT FILES:\n{context}\n\n{instr}")
    if fail:
        p += (f"\n\nYour previous attempt FAILED verification:\n{fail}\n\n"
              f"Fix it and return the corrected changes in the same {fmt}.")
    return p


# --------------------------------------------------------------------- results
@dataclass
class Candidate:
    name: str
    label: str
    edits: Dict[str, str]
    raw: str
    verdict: Optional[Verdict] = None


@dataclass
class EditResult:
    status: str                                  # verified | failed | unverified | no-agents
    detail: str = ""
    winner: Optional[str] = None
    diff: str = ""
    edited: Optional[Dict[str, str]] = None      # full winning/selected file map (base + edits)
    edits: Optional[Dict[str, str]] = None       # just the changed files (what apply() writes)
    rounds: int = 0
    summary: List[Tuple[str, str, str]] = field(default_factory=list)  # (label, status, detail)

    @property
    def verified(self) -> bool:
        return self.status == "verified"


# ----------------------------------------------------------------------- engine
class EditJob:
    """Run generate-verify-select over a repo and return a reviewable EditResult.

    Never writes to `repo_root` — call apply_edits(repo_root, result.edits) after the user accepts.
    """

    def __init__(self, agents: List[Agent], oracle: Optional[Oracle] = None,
                 console: Optional[Console] = None, sandbox: Optional[Sandbox] = None,
                 rounds: int = 3, max_context_bytes: int = _MAX_CONTEXT_BYTES,
                 diff_mode: bool = False) -> None:
        self.agents = agents
        self.oracle = oracle
        self.console = console or Console()
        self._sandbox = sandbox
        self.rounds = max(1, rounds)
        self.max_context_bytes = max_context_bytes
        self.diff_mode = diff_mode   # section-replace format: 60-80% fewer output tokens for small edits
        self.round_log: List[List[Candidate]] = []   # candidates (with verdicts) per round, for the benchmark

    async def _propose(self, agent: Agent, task: str, base: Dict[str, str],
                       fail: Optional[str]) -> Candidate:
        prompt = _gen_prompt(task, base, self.max_context_bytes, fail, diff_mode=self.diff_mode)
        try:
            raw = await agent.complete_raw([{"role": "user", "content": prompt}])
        except Exception as e:                   # one head failing must not kill the job
            return Candidate(agent.name, agent.label, {}, "", Verdict("fail", f"{type(e).__name__}: {e}"))
        if self.diff_mode:
            edits = parse_section_edits(raw, base)
        else:
            edits = parse_edits(raw)
        return Candidate(agent.name, agent.label, edits, raw)

    async def run(self, task: str, repo_root) -> EditResult:
        if not self.agents:
            return EditResult("no-agents", "no agents available (check API keys / roster)")
        base = read_repo(repo_root)
        oracle = self.oracle or AbsentOracle()
        self.round_log = []
        ctx_text, included = build_context(task, base, self.max_context_bytes)
        if len(included) < len(base):
            self.console.print(f"[dim]context: sent {len(included)}/{len(base)} files by relevance "
                               f"(~{est_tokens(ctx_text)} tok); {len(base) - len(included)} listed by name "
                               f"only — recall-over-re-read for the repo.[/dim]")
        mode_label = "section-replace (diff_mode — emit hunks only)" if self.diff_mode else "whole-file"
        self.console.print(f"[dim]code: edit format = {mode_label}[/dim]")
        self.console.print(f"[dim]code: oracle = {oracle.describe()}[/dim]")

        # ---- no independent oracle: selection only, never executed/graded ----
        if not oracle.independent:
            cands = await asyncio.gather(*[self._propose(a, task, base, None) for a in self.agents])
            summary = [(c.label, "unverified", f"{len(c.edits)} file(s)") for c in cands]
            pick = next((c for c in cands if c.edits), None)
            self.console.print("[yellow]⚠ UNVERIFIED — no oracle. Selection only: review the diff and "
                               "decide; nothing was executed or checked. Set one with "
                               "--oracle / /oracle (e.g. \"pytest -q\").[/yellow]")
            if pick is None:
                return EditResult("unverified", "no candidate produced any edits", summary=summary)
            edited = {**base, **pick.edits}
            return EditResult("unverified", "selection only (no oracle ran)", pick.label,
                              build_diff(base, edited), edited, pick.edits, 1, summary)

        # ---- oracle present: generate -> verify -> select -> critique-revise ----
        sandbox = self._sandbox or Sandbox()
        self.console.print(f"[dim]code: sandbox = {sandbox.note}[/dim]")
        last_fail: Dict[str, str] = {}
        summary: List[Tuple[str, str, str]] = []
        for rnd in range(1, self.rounds + 1):
            self.console.rule(f"[bold]code — round {rnd}/{self.rounds}[/bold]")
            cands = await asyncio.gather(
                *[self._propose(a, task, base, last_fail.get(a.name)) for a in self.agents])
            summary = []
            for c in cands:
                if c.verdict is None:
                    c.verdict = (Verdict("fail", "no edits in the FILE: format")
                                 if not c.edits else oracle.check_workspace({**base, **c.edits}, sandbox))
                mark = {"pass": "[green]✓ pass[/green]", "fail": "[red]✗ fail[/red]",
                        "unverified": "[yellow]? unverified[/yellow]"}[c.verdict.status]
                self.console.print(f"  {c.label}: {mark}  [dim]{len(c.edits)} file(s) · {c.verdict.detail}[/dim]")
                summary.append((c.label, c.verdict.status, c.verdict.detail))
            self.round_log.append(list(cands))

            passers = [c for c in cands if c.verdict and c.verdict.passed]
            if passers:
                c = passers[0]
                edited = {**base, **c.edits}
                extra = "" if len(passers) == 1 else f" ({len(passers)} passed; picked {c.label}, first in order)"
                self.console.rule(f"[bold green]VERIFIED — {c.label} passed{extra}[/bold green]")
                return EditResult("verified", c.verdict.detail, c.label,
                                  build_diff(base, edited), edited, c.edits, rnd, summary)

            last_fail = {c.name: (c.verdict.detail if c.verdict else "") for c in cands}

        self.console.rule("[bold red]code — no candidate passed[/bold red]")
        return EditResult("failed", f"0/{len(self.agents)} passed after {self.rounds} round(s)",
                          rounds=self.rounds, summary=summary)
