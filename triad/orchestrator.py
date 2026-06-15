"""Collaboration modes.

  parallel  — same task to everyone at once; independent answers, side by side.
  relay     — agents work in sequence, each building on the running transcript.
  council   — everyone answers, then a chair agent synthesizes the best single answer.

When `protocol` is on, relay/council pass compact structured handoffs (see protocol.py)
instead of re-sending the whole transcript, and report how much context that saved.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from rich.console import Console

from .agents import Agent
from .oracle import AbsentOracle, extract_code
from .protocol import PROTOCOL_INSTRUCTION, RefStore, compact_block, est_tokens
from .sandbox import Sandbox
from .ui import live_parallel, live_single


class Orchestrator:
    def __init__(self, agents: List[Agent], skills: Dict, console: Console,
                 mode: str = "parallel", chair: Optional[str] = None) -> None:
        self.agents = agents
        self.skills = skills
        self.console = console
        self.mode = mode
        self.chair = chair or (agents[0].name if agents else None)
        self.protocol = False
        self.refs = RefStore()
        self.transcript: List[str] = []  # chronological session log for /save (all modes)
        self.oracle = None               # verify-mode pass condition; None -> unverified (selection only)
        self.memory = None               # optional VaultMemory: recall relevant notes instead of re-sending all
        self.history_limit = 0           # >0: keep only the last N exchanges per agent (older lives in the vault)
        self.on_evict = None             # optional sink(agent, dropped_messages): archive trimmed turns to the vault

    def _get(self, name: str) -> Agent:
        for a in self.agents:
            if a.name == name:
                return a
        return self.agents[0]

    def _record(self, task: str, outputs: List) -> None:
        """Append one turn to the session transcript so /save captures every mode.

        relay/council stream via stream_raw and never touch agent.history, so without
        this their output would be lost on save. outputs: list of (label, text).
        """
        block = [f"## [{self.mode}] {task}", ""]
        for label, text in outputs:
            block.append(f"### {label}")
            block.append((text or "").strip())
            block.append("")
        self.transcript.append("\n".join(block).rstrip() + "\n")

    def _savings(self, baseline_chars: int, actual_chars: int) -> None:
        """Report compaction of the context handed between agents (raw vs digest)."""
        base, act = est_tokens("x" * baseline_chars), est_tokens("x" * actual_chars)
        if base <= 0:
            return
        pct = 100 * (base - act) / base
        self.console.print(
            f"[dim]protocol: ~{act} tokens of handoff context vs ~{base} raw "
            f"(~{pct:.0f}% less passed between agents). Grows with longer chats.[/dim]"
        )

    async def dispatch(self, task: str) -> None:
        if self.mode == "relay":
            await self.run_relay(task)
        elif self.mode == "council":
            await self.run_council(task)
        elif self.mode == "verify":
            await self.run_verify(task)
        else:
            await self.run_parallel(task)

    # ----------------------------------------------------------------- modes
    def _trim_history(self, agent) -> None:
        """Keep only the last `history_limit` exchanges per agent; older turns persist in the vault
        and are pulled back on demand by recall. This is what stops the re-sent context from growing."""
        keep = self.history_limit * 2  # one exchange == user + assistant
        if keep and len(agent.history) > keep:
            dropped = agent.history[:-keep]
            agent.history = agent.history[-keep:]
            if self.on_evict and dropped:
                try:
                    self.on_evict(agent, dropped)   # summarize-on-evict: keep it recallable from the vault
                except Exception:
                    pass  # archiving is best-effort; never break a turn over it

    async def run_parallel(self, task: str) -> None:
        # Parallel has no handoff to compress, so protocol doesn't apply here — but persistent
        # history does grow, so this is where recall-over-re-read + bounded history pay off.
        ctx = self.memory.recall(task) if self.memory else ""
        if ctx:
            cap = self.history_limit or "∞"
            self.console.print(f"[dim]memory: recalled ~{est_tokens(ctx)} tokens of relevant context "
                               f"from the vault (history capped at {cap} turn(s)).[/dim]")
        buffers = await live_parallel(self.console, self.agents, lambda a: a.stream(task, context=ctx))
        self._record(task, [(a.label, buffers.get(a.name, "")) for a in self.agents])
        if self.history_limit:
            for a in self.agents:
                self._trim_history(a)

    async def run_relay(self, task: str) -> None:
        raw_accum = ""          # full outputs concatenated (what a full transcript passes)
        note_accum = ""         # compact notes concatenated (what protocol passes)
        baseline_chars = actual_chars = 0
        outputs = []            # (label, text) per step, for the session transcript

        for i, a in enumerate(self.agents):
            if self.protocol:
                if i == 0:
                    prompt = f"TASK:\n{task}\n\n{PROTOCOL_INSTRUCTION}"
                else:
                    prompt = (
                        f"TASK:\n{task}\n\nPRIOR WORK (compact handoffs):\n{note_accum.strip()}\n\n"
                        f"Add your contribution, building on the above.\n\n{PROTOCOL_INSTRUCTION}"
                    )
            else:
                if i == 0:
                    prompt = task
                else:
                    prompt = (
                        "You are collaborating with other AI agents on a shared task.\n\n"
                        f"TASK:\n{task}\n\nWORK SO FAR (from other agents):\n{raw_accum.strip()}\n\n"
                        "Add your contribution: build on it, fix mistakes, fill gaps. "
                        "Don't just repeat what's already there."
                    )

            if i > 0:  # context re-passed at this hop, raw vs compact
                baseline_chars += len(raw_accum)
                actual_chars += len(note_accum)

            text = await live_single(
                self.console, a, a.stream_raw([{"role": "user", "content": prompt}]),
                f"{a.label} — relay step {i + 1}/{len(self.agents)}",
            )

            self.refs.put(f"relay/{i + 1}", text)
            outputs.append((a.label, text))
            raw_accum += f"\n\n## {a.label}\n{text}"
            if self.protocol:
                note_accum += "\n\n" + compact_block(text, a.label)

        self._record(task, outputs)
        if self.protocol:
            self._savings(baseline_chars, actual_chars)

    async def run_council(self, task: str) -> None:
        self.console.rule("[bold]Round 1 — independent answers[/bold]")
        instr = f"{task}\n\n{PROTOCOL_INSTRUCTION}" if self.protocol else task
        answers = await live_parallel(
            self.console, self.agents,
            lambda a: a.stream_raw([{"role": "user", "content": instr}]),
        )

        if self.protocol:
            blocks, baseline_chars, actual_chars = [], 0, 0
            for i, a in enumerate(self.agents):
                self.refs.put(f"council/{a.name}", answers[a.name])
                block = compact_block(answers[a.name], f"Response {chr(65 + i)}")
                blocks.append(block)
                actual_chars += len(block)
                baseline_chars += len(answers[a.name])
            body = "\n\n".join(blocks)
        else:
            body = "\n\n".join(
                f"### Response {chr(65 + i)}\n{answers[a.name]}" for i, a in enumerate(self.agents)
            )

        chair = self._get(self.chair)
        self.console.rule(f"[bold]Round 2 — {chair.label} synthesizes[/bold]")
        synth = (
            "Several AI agents independently answered the task below. As the chair, "
            "produce the single best final answer: merge their strengths, resolve "
            "disagreements, correct errors, and flag any important dissent.\n\n"
            f"TASK:\n{task}\n\nRESPONSES:\n{body}"
        )
        synth_text = await live_single(
            self.console, chair, chair.stream_raw([{"role": "user", "content": synth}]),
            f"{chair.label} — synthesis",
        )
        outputs = [(a.label, answers[a.name]) for a in self.agents]
        outputs.append((f"{chair.label} — synthesis", synth_text))
        self._record(task, outputs)
        if self.protocol:
            self._savings(baseline_chars, actual_chars)

    # ----------------------------------------------------------------- verify
    @staticmethod
    def _gen_prompt(task: str, fail: Optional[str] = None) -> str:
        p = (f"TASK:\n{task}\n\nReturn a complete, self-contained solution as ONE fenced code "
             "block (```), and nothing outside the block.")
        if fail:
            p += (f"\n\nYour previous attempt FAILED verification:\n{fail}\n\nFix it and return the "
                  "full corrected solution as one code block.")
        return p

    async def run_verify(self, task: str, rounds: int = 3) -> None:
        """generate-verify-select. N candidates -> independent oracle -> pick a passer, else
        critique-revise (bounded). With NO independent oracle: generate and report UNVERIFIED
        (selection only) — never a self-grade, never a fabricated pass."""
        oracle = self.oracle or AbsentOracle()
        sandbox = Sandbox()
        self.console.print(f"[dim]verify: oracle = {oracle.describe()}[/dim]")
        self.console.print(f"[dim]verify: sandbox = {sandbox.note}[/dim]")

        # ---- no independent oracle: selection only ----
        if not oracle.independent:
            self.console.rule("[bold]verify — generate (no oracle)[/bold]")
            gen = await live_parallel(
                self.console, self.agents,
                lambda a: a.stream_raw([{"role": "user", "content": self._gen_prompt(task)}]))
            outputs = [(a.label, gen.get(a.name, "")) for a in self.agents]
            outputs.append(("verify", "UNVERIFIED — selection only (no oracle ran; nothing executed/checked)"))
            self._record(task, outputs)
            self.console.print("[yellow]⚠ UNVERIFIED — no oracle. Selection only: review the candidates "
                               "above and choose; nothing was executed or checked. Set one with "
                               "[bold]/oracle <cmd>[/bold] (e.g. /oracle \"pytest -q\").[/yellow]")
            return

        # ---- oracle present: generate -> verify -> select -> critique-revise ----
        last_fail: Dict[str, str] = {}
        for rnd in range(1, rounds + 1):
            self.console.rule(f"[bold]verify — round {rnd}/{rounds}[/bold]")
            gen = await live_parallel(
                self.console, self.agents,
                lambda a: a.stream_raw([{"role": "user",
                                         "content": self._gen_prompt(task, last_fail.get(a.name))}]))
            results = []
            for a in self.agents:
                v = oracle.check(gen.get(a.name, ""), sandbox)
                results.append((a, extract_code(gen.get(a.name, "")), v))
                mark = {"pass": "[green]✓ pass[/green]", "fail": "[red]✗ fail[/red]",
                        "unverified": "[yellow]? unverified[/yellow]"}[v.status]
                self.console.print(f"  {a.label}: {mark}  [dim]{v.detail}[/dim]")

            passers = [(a, code, v) for (a, code, v) in results if v.passed]
            if passers:
                a, code, v = passers[0]
                extra = "" if len(passers) == 1 else f" ({len(passers)} passed; picked {a.label}, first in order)"
                self.console.rule(f"[bold green]VERIFIED — {a.label} passed{extra}[/bold green]")
                self.console.print(code)
                self._record(task, [(a.label, code),
                                    ("verify", f"PASSED — {a.label} round {rnd} ({v.detail})")])
                return

            last_fail = {a.name: v.detail for (a, code, v) in results}
            if rnd == rounds:
                self.console.rule("[bold red]verify — no candidate passed[/bold red]")
                self.console.print(f"[red]0/{len(self.agents)} passed after {rounds} round(s). "
                                   "Candidates recorded; nothing verified.[/red]")
                outputs = [(a.label, code) for (a, code, v) in results]
                outputs.append(("verify", f"FAILED — 0/{len(self.agents)} passed after {rounds} rounds"))
                self._record(task, outputs)
