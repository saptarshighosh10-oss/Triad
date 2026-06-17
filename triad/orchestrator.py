"""Collaboration modes.

  parallel  — same task to everyone at once; independent answers, side by side.
  relay     — agents work in sequence, each building on the running transcript.
  council   — everyone answers, then a chair agent synthesizes the best single answer.

When `protocol` is on, relay/council pass compact structured handoffs (see protocol.py)
instead of re-sending the whole transcript, and report how much context that saved.
"""
from __future__ import annotations

from itertools import combinations
from typing import Dict, List, Optional

from rich.console import Console

from .agents import Agent, build_free_swarm
from .oracle import AbsentOracle, extract_code
from .protocol import (PROTOCOL_INSTRUCTION, DIALECT_INSTRUCTION, NOFLUFF_INSTRUCTION,
                       RefStore, compact_block, est_tokens)
from . import config as _config
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
        self.protocol = True             # on by default: relay/council pass compact handoffs not raw transcripts
        self.dialect = False             # compressed agent dialect in handoffs (extra ~40% cut, opt-in)
        self.refs = RefStore()
        self.transcript: List[str] = []  # chronological session log for /save (all modes)
        self.oracle = None               # verify-mode pass condition; None -> unverified (selection only)
        self.memory = None               # optional VaultMemory: recall relevant notes instead of re-sending all
        self.history_limit = 6           # keep only last N exchanges per agent; older turns archived to vault
        self.on_evict = None             # optional sink(agent, dropped_messages): archive trimmed turns to the vault
        self.max_depth = 2               # swarm mode: how many levels a director may recursively decompose

    @staticmethod
    def _jaccard(a: str, b: str) -> float:
        """Word-level Jaccard similarity — fast, no deps, good enough for consensus detection."""
        wa, wb = set(a.lower().split()), set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / len(wa | wb)

    def _consensus(self, answers: dict, threshold: float = 0.75) -> bool:
        """True if every pair of agent answers is above the similarity threshold."""
        texts = [t for t in answers.values() if t]
        if len(texts) < 2:
            return False
        return all(self._jaccard(a, b) >= threshold for a, b in combinations(texts, 2))

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
        mode = self.mode
        if mode == "auto":
            tier = _config.classify_task(task)
            if tier["tier"] != "code":
                mode = "parallel"                      # not code → just answer in parallel
            elif _config.is_multipart(task):
                mode = "swarm"                         # several independent pieces → split them
            elif self.oracle:
                mode = "verify"                        # one coupled artifact + a test → retry loop
            else:
                mode = "plan-execute"                  # code, no test → director + free workers
            self.console.print(f"[dim]auto: tier={tier['tier']}"
                               f"{' · multipart' if _config.is_multipart(task) else ''}"
                               f"{' · oracle' if self.oracle else ''} → mode={mode}[/dim]")
        if mode == "relay":
            await self.run_relay(task)
        elif mode == "council":
            await self.run_council(task)
        elif mode == "verify":
            await self.run_verify(task)
        elif mode == "plan-execute":
            await self.run_plan_execute(task)
        elif mode == "swarm":
            await self.run_swarm(task)
        else:
            await self.run_parallel(task)

    # ----------------------------------------------------------------- modes
    def _trim_history(self, agent) -> None:
        """Keep only the last `history_limit` exchanges per agent; older turns persist in the vault
        and are pulled back on demand by recall. This is what stops the re-sent context from growing.

        Also builds a rolling summary from dropped user messages — injected as ephemeral context
        on the next turn so the agent retains thread awareness without re-sending the full history.
        """
        keep = self.history_limit * 2  # one exchange == user + assistant
        if keep and len(agent.history) > keep:
            dropped = agent.history[:-keep]
            agent.history = agent.history[-keep:]
            # Rolling summary: condense dropped turns into a compact thread reminder (~60 tokens).
            # $0 — no LLM call, just the user-side of each turn truncated to 100 chars.
            prior = [m["content"][:100] for m in dropped if m.get("role") == "user"]
            if prior:
                agent._rolling_summary = "Earlier in this session: " + " → ".join(prior)
            if self.on_evict and dropped:
                try:
                    self.on_evict(agent, dropped)   # summarize-on-evict: keep it recallable from the vault
                except Exception:
                    pass  # archiving is best-effort; never break a turn over it

    async def run_parallel(self, task: str) -> None:
        tier = _config.classify_task(task)
        budget = tier["budget_hint"]
        max_tok = tier["max_tokens"]
        cot = f" {tier['cot_hint']}" if tier.get("cot_hint") else ""
        fmt = f" {tier['format_hint']}" if tier.get("format_hint") else ""
        self.console.print(f"[dim]tier: {tier['tier']} → cap {max_tok} output tokens.[/dim]")
        ctx = self.memory.recall(task) if self.memory else ""
        if ctx:
            self.console.print(f"[dim]memory: recalled ~{est_tokens(ctx)} tokens.[/dim]")
        task_with_budget = f"{task}\n\n[Reply in {budget}.{cot}{fmt} {NOFLUFF_INSTRUCTION}]"
        buffers = await live_parallel(self.console, self.agents,
                                      lambda a: a.stream(task_with_budget, context=ctx))

        # Auto-retry agents with bad output (too short, refusal, truncated, missing code block)
        bad = [(a, r) for a in self.agents
               for ok, r in [_config.is_bad_output(buffers.get(a.name, ""), tier["tier"])] if ok]
        if bad:
            names = ", ".join(a.label for a, _ in bad)
            self.console.print(f"[dim]retry: bad output from {names} — retrying once[/dim]")
            retry_prompt = (f"{task}\n\n[Previous attempt was unusable. Try again.{fmt} "
                            f"Reply in {budget}. {NOFLUFF_INSTRUCTION}]")
            retry_buf = await live_parallel(
                self.console, [a for a, _ in bad],
                lambda a: a.stream_raw([{"role": "user", "content": retry_prompt}],
                                       max_tokens=max_tok),
            )
            for a, _ in bad:
                buffers[a.name] = retry_buf.get(a.name, buffers.get(a.name, ""))

        for a in self.agents:
            for i in range(len(a.history) - 1, -1, -1):
                if (a.history[i].get("role") == "user"
                        and a.history[i].get("content", "").startswith(task)):
                    a.history[i] = {"role": "user", "content": task}
                    break
        self._record(task, [(a.label, buffers.get(a.name, "")) for a in self.agents])
        if self.history_limit:
            for a in self.agents:
                self._trim_history(a)

    async def run_relay(self, task: str) -> None:
        raw_accum = ""          # full outputs concatenated (what a full transcript passes)
        note_accum = ""         # compact notes concatenated (what protocol passes)
        baseline_chars = actual_chars = 0
        outputs = []            # (label, text) per step, for the session transcript
        tier = _config.classify_task(task)
        budget = tier["budget_hint"]
        max_tok = tier["max_tokens"]
        dialect_instr = f"\n\n{DIALECT_INSTRUCTION}" if self.dialect else ""
        nofluff = f" {NOFLUFF_INSTRUCTION}"

        # Task dedup: hop 0 puts the task in the user message; hops 1+ move it to the
        # system prompt so it isn't re-sent in the (growing) user body every hop.
        task_system = f"TASK (for all hops):\n{task}"

        for i, a in enumerate(self.agents):
            if self.protocol:
                proto = f"{PROTOCOL_INSTRUCTION}{dialect_instr}"
                if i == 0:
                    prompt = f"TASK:\n{task}\n\n[Reply in {budget}.{nofluff}]\n\n{proto}"
                    sys_override = None
                else:
                    prompt = (
                        f"PRIOR WORK (compact handoffs):\n{note_accum.strip()}\n\n"
                        f"Add your contribution, building on the above.\n\n[Reply in {budget}.{nofluff}]\n\n{proto}"
                    )
                    sys_override = task_system  # task lives here, not in the user body
            else:
                if i == 0:
                    prompt = f"{task}\n\n[Reply in {budget}.{nofluff}]"
                    sys_override = None
                else:
                    prompt = (
                        f"WORK SO FAR (from other agents):\n{raw_accum.strip()}\n\n"
                        f"Add your contribution: build on it, fix mistakes, fill gaps. "
                        f"Don't just repeat what's already there.\n\n[Reply in {budget}.{nofluff}]"
                    )
                    sys_override = task_system

            if i > 0:  # context re-passed at this hop, raw vs compact
                baseline_chars += len(raw_accum)
                actual_chars += len(note_accum)

            text = await live_single(
                self.console, a,
                a.stream_raw([{"role": "user", "content": prompt}],
                             system=sys_override, max_tokens=max_tok),
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
        tier = _config.classify_task(task)
        budget = tier["budget_hint"]
        max_tok = tier["max_tokens"]
        dialect_instr = f"\n\n{DIALECT_INSTRUCTION}" if self.dialect else ""
        nofluff = f" {NOFLUFF_INSTRUCTION}"
        if self.protocol:
            instr = (f"{task}\n\n[Reply in {budget}.{nofluff}]\n\n"
                     f"{PROTOCOL_INSTRUCTION}{dialect_instr}")
        else:
            instr = f"{task}\n\n[Reply in {budget}.{nofluff}]"
        answers = await live_parallel(
            self.console, self.agents,
            lambda a: a.stream_raw([{"role": "user", "content": instr}], max_tokens=max_tok),
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

        # Early exit: if all agents converge (high pairwise similarity), skip the synthesis
        # call entirely — it would just repeat what everyone already said, wasting a full API call.
        if self._consensus(answers):
            winner = next(iter(answers.values()))
            self.console.print(
                f"[dim]council: consensus detected (all answers >{75}% similar) — "
                f"skipping synthesis, returning first response. Saved 1 API call.[/dim]"
            )
            outputs = [(a.label, answers[a.name]) for a in self.agents]
            outputs.append(("synthesis", f"[skipped — agents converged]\n\n{winner}"))
            self._record(task, outputs)
            if self.protocol:
                self._savings(baseline_chars, actual_chars)
            return

        chair = self._get(self.chair)
        self.console.rule(f"[bold]Round 2 — {chair.label} synthesizes[/bold]")
        synth = (
            "Several AI agents independently answered the task below. As the chair, "
            "produce the single best final answer: merge their strengths, resolve "
            "disagreements, correct errors, and flag any important dissent.\n\n"
            f"TASK:\n{task}\n\nRESPONSES:\n{body}"
        )
        synth_text = await live_single(
            self.console, chair,
            chair.stream_raw([{"role": "user", "content": synth}], max_tokens=max_tok),
            f"{chair.label} — synthesis",
        )
        outputs = [(a.label, answers[a.name]) for a in self.agents]
        outputs.append((f"{chair.label} — synthesis", synth_text))
        self._record(task, outputs)
        if self.protocol:
            self._savings(baseline_chars, actual_chars)

    # ----------------------------------------------------------- plan-execute
    async def run_plan_execute(self, task: str) -> None:
        """Smart director plans → free workers execute in parallel → oracle verifies.

        Token shape:
          director (1 call, ~300 tok out) → free heads (1-2 rounds, tight spec = high pass rate)

        Cheaper than free-leads for complex tasks because:
          - director writes a better spec in fewer tokens than free fumbling
          - free models follow clear specs reliably → fewer oracle retry rounds
          - each retry re-sends full input context, so cutting rounds is the biggest win
        """
        if not self.agents:
            self.console.print("[red]no agents available[/red]")
            return

        # Split into director (first agent) + workers (the rest).
        # Single-agent case: skip planning (director would plan then execute alone — wasteful).
        if len(self.agents) == 1:
            self.console.print("[dim]plan-execute: single agent — skipping planning step, running parallel.[/dim]")
            await self.run_parallel(task)
            return
        director = self.agents[0]
        # Prefer free swarm as workers — director fans out to up to 5 free models.
        # Falls back to self.agents[1:] if no OpenRouter key is set.
        swarm = build_free_swarm(5)
        workers = swarm if swarm else self.agents[1:]
        if swarm:
            self.console.print(f"[dim]swarm: {len(swarm)} free workers "
                               f"({', '.join(a.label for a in swarm)})[/dim]")

        self.console.rule(f"[bold]plan-execute — {director.label} plans[/bold]")

        # ---- Step 1: director writes a tight spec ----
        plan_prompt = (
            f"You are the director. A team of AI workers will implement this task based ONLY on "
            f"your spec — they won't see the original request.\n\n"
            f"TASK:\n{task}\n\n"
            f"Write a tight implementation spec in ≤250 tokens:\n"
            f"- Exact inputs, outputs, edge cases\n"
            f"- Key constraints and approach\n"
            f"- What a correct result looks like\n\n"
            f"No code. No preamble. Dense noun phrases. Be the spec, not the solution."
        )
        spec = await live_single(
            self.console, director,
            director.stream_raw([{"role": "user", "content": plan_prompt}], max_tokens=300),
            f"{director.label} — writing spec",
        )
        self.console.print(f"[dim]spec: ~{est_tokens(spec)} tokens[/dim]")

        # ---- Step 2: free workers execute the spec in parallel ----
        self.console.rule("[bold]plan-execute — workers implement[/bold]")
        oracle = self.oracle or AbsentOracle()
        sandbox = Sandbox()

        tier = _config.classify_task(task)
        cot = f" {tier['cot_hint']}" if tier.get("cot_hint") else ""
        fmt = f" {tier['format_hint']}" if tier.get("format_hint") else ""
        execute_prompt = (
            f"Implement the following spec exactly. Return only the solution.\n\n"
            f"SPEC:\n{spec}\n\n"
            f"[{NOFLUFF_INSTRUCTION}{cot}{fmt}]"
        )

        if not oracle.independent:
            # No oracle — just run workers and return all outputs
            buffers = await live_parallel(
                self.console, workers,
                lambda a: a.stream_raw([{"role": "user", "content": execute_prompt}], max_tokens=1024),
            )
            outputs = [(a.label, buffers.get(a.name, "")) for a in workers]
            outputs.insert(0, (f"{director.label} — spec", spec))
            self._record(task, outputs)
            self.console.print(
                "[yellow]⚠ UNVERIFIED — no oracle. Set one with [bold]/oracle <cmd>[/bold] "
                "to enable verified selection.[/yellow]"
            )
            return

        # ---- Step 3: oracle verifies each worker's output ----
        self.console.print(f"[dim]oracle = {oracle.describe()}[/dim]")
        self.console.print(f"[dim]sandbox = {sandbox.note}[/dim]")

        last_fail: Dict[str, str] = {}
        for rnd in range(1, 4):  # max 3 rounds
            self.console.rule(f"[bold]plan-execute — verify round {rnd}[/bold]")

            if last_fail:
                retry_parts = []
                for a in workers:
                    fail_note = last_fail.get(a.name, "")
                    retry_parts.append((a, (
                        f"{execute_prompt}\n\nYour previous attempt FAILED:\n{fail_note}\n\nFix it."
                    )))
                prompts = {a.name: p for a, p in retry_parts}
            else:
                prompts = {a.name: execute_prompt for a in workers}

            results = await live_parallel(
                self.console, workers,
                lambda a: a.stream_raw(
                    [{"role": "user", "content": prompts[a.name]}], max_tokens=1024
                ),
            )

            passers = []
            last_fail = {}
            for a in workers:
                raw = results.get(a.name, "")
                verdict = oracle.check(raw, sandbox)
                mark = {"pass": "[green]✓[/green]", "fail": "[red]✗[/red]",
                        "unverified": "[yellow]?[/yellow]"}[verdict.status]
                self.console.print(f"  {a.label}: {mark}  [dim]{verdict.detail}[/dim]")
                if verdict.passed:
                    passers.append((a, raw, verdict))
                else:
                    last_fail[a.name] = verdict.detail

            if passers:
                a, raw, verdict = passers[0]
                self.console.rule(f"[bold green]VERIFIED — {a.label} passed[/bold green]")
                self.console.print(extract_code(raw))
                self._record(task, [
                    (f"{director.label} — spec", spec),
                    (a.label, raw),
                    ("verify", f"PASSED — {a.label} round {rnd} ({verdict.detail})"),
                ])
                return

            if rnd == 3:
                self.console.rule("[bold red]plan-execute — no worker passed[/bold red]")
                self.console.print(f"[red]0/{len(workers)} passed after 3 rounds.[/red]")
                self._record(task, [
                    (f"{director.label} — spec", spec),
                    *[(a.label, results.get(a.name, "")) for a in workers],
                    ("verify", "FAILED — 0 passed"),
                ])

    # ------------------------------------------------------------------ swarm
    async def run_swarm(self, task: str) -> None:
        """Recursive fan-out: director decomposes → subtasks solved by free workers, or split
        again if still complex (bounded by max_depth). Results integrate back up the tree.

        This is the multi-level version of plan-execute: a worker can itself become a director
        for its own sub-piece, so a deeply nested task is handled like a real subagent tree —
        but every leaf runs on free models, so the depth costs labor, not money.
        """
        if not self.agents:
            self.console.print("[red]no agents available[/red]")
            return
        result = await self._swarm_node(task, depth=0)
        # If an oracle is set, verify the INTEGRATED result — the tree's pieces only count if
        # they actually fit together and pass. Per-leaf checks can't catch interface mismatch.
        verdict_line = ""
        if self.oracle and self.oracle.independent:
            v = self.oracle.check(result, Sandbox())
            verdict_line = f"\n\n[oracle] {'PASS' if v.passed else 'FAIL'} — {v.detail}"
            rule = "[bold green]swarm — VERIFIED[/bold green]" if v.passed else "[bold red]swarm — FAILED[/bold red]"
        else:
            rule = "[bold green]swarm — done[/bold green]"
        self._record(task, [("swarm result", result + verdict_line)])
        self.console.rule(rule)
        self.console.print(result)
        if verdict_line:
            self.console.print(verdict_line)

    async def _swarm_node(self, task: str, depth: int) -> str:
        """One node in the tree: either a leaf (free workers solve it) or a split (recurse)."""
        indent = "  " * depth
        # Leaf: at max depth, or director judges the task atomic → free workers solve directly.
        subtasks = [] if depth >= self.max_depth else await self._decompose(task, depth)
        if not subtasks:
            self.console.print(f"[dim]{indent}leaf (d{depth}): {task[:70]}[/dim]")
            return await self._swarm_leaf(task)

        self.console.print(f"[dim]{indent}split (d{depth}) → {len(subtasks)} subtasks[/dim]")
        results = []
        for sub in subtasks:
            r = await self._swarm_node(sub, depth + 1)   # recurse: a subtask may split again
            results.append((sub, r))
        # Guard: if every subtask came back empty (all workers throttled/errored), don't ask the
        # director to "integrate" from nothing — it will hallucinate. Fail loudly instead.
        if not any(r.strip() for _, r in results):
            self.console.print(f"[red]{indent}all {len(subtasks)} subtasks failed "
                               f"(workers errored/throttled) — nothing to integrate[/red]")
            return ""
        return await self._integrate(task, results)

    async def _decompose(self, task: str, depth: int) -> List[str]:
        """Director splits a task into ≤3 independent subtasks — or [] if it's atomic.

        [] means 'don't split, just solve it' — that's the recursion's base case, decided by the
        director, not a fixed rule. Free models over-split, so this stays on the first (smart) head.
        """
        director = self.agents[0]
        prompt = (
            f"Break this task into 2-3 INDEPENDENT subtasks that can be solved separately, then "
            f"combined. If it's already simple enough to solve in one go, reply with exactly ATOMIC.\n\n"
            f"TASK:\n{task}\n\n"
            f"Reply as a numbered list (1. 2. 3.), one subtask per line, nothing else. "
            f"Or the single word ATOMIC."
        )
        out = await live_single(
            self.console, director,
            director.stream_raw([{"role": "user", "content": prompt}], max_tokens=300),
            f"{director.label} — decomposing (depth {depth})",
        )
        if "atomic" in out.lower()[:40] and len(out.strip()) < 40:
            return []
        subs = []
        for line in out.splitlines():
            line = line.strip()
            if line and line[0].isdigit():
                subs.append(line.lstrip("0123456789.) ").strip())
        return [s for s in subs if s][:3]

    async def _swarm_leaf(self, task: str) -> str:
        """Solve one atomic subtask on the free swarm; oracle-pick if set, else longest answer.

        Workers are spread across providers (build_free_swarm) so a fanned-out tree doesn't
        rate-limit itself. Error/throttle responses are filtered out — live_parallel records a
        failed stream as '[error] ...' text, which must never be mistaken for a real answer.
        """
        from .oracle import AbsentOracle
        workers = build_free_swarm(3) or self.agents[1:] or self.agents
        tier = _config.classify_task(task)
        cot = f" {tier['cot_hint']}" if tier.get("cot_hint") else ""
        prompt = f"{task}\n\n[{NOFLUFF_INSTRUCTION}{cot}]"
        results = await live_parallel(
            self.console, workers,
            lambda a: a.stream_raw([{"role": "user", "content": prompt}], max_tokens=tier["max_tokens"]),
        )
        cands = []
        for a in workers:
            text = results.get(a.name, "").strip()
            if text and "[error]" not in text:   # drop throttle/error responses, not real output
                cands.append((a, text))
        if not cands:
            return ""   # signal total failure — the caller must NOT integrate from nothing
        oracle = self.oracle or AbsentOracle()
        if oracle.independent:
            sandbox = Sandbox()
            for a, raw in cands:
                if oracle.check(raw, sandbox).passed:
                    return raw
        return max(cands, key=lambda c: len(c[1]))[1]   # no oracle → longest (proxy for most complete)

    async def _integrate(self, task: str, results: List) -> str:
        """Director merges subtask results into one coherent answer for the parent task."""
        director = self.agents[0]
        body = "\n\n".join(f"### Subtask: {sub}\n{res}" for sub, res in results)
        prompt = (
            f"These subtasks were solved independently. Combine them into one coherent, complete "
            f"solution to the original task. Resolve overlaps and fill gaps.\n\n"
            f"ORIGINAL TASK:\n{task}\n\nSUBTASK RESULTS:\n{body}"
        )
        return await live_single(
            self.console, director,
            director.stream_raw([{"role": "user", "content": prompt}], max_tokens=4096),
            f"{director.label} — integrating",
        )

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
