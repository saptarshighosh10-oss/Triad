# stage 2 — verify-select

Design for triad's [[generate-verify-select]] mode. Saved before the gate/build so it survives a cutoff. Back to [[index]].

## Goal
Make the free roster *trustworthy* on **verifiable** tasks. Three free chat opinions make correlated errors and then agree ([[cross-provider decorrelation]] mitigates the correlation but doesn't remove it). The fix is an executable check, not more opinions.

## The loop (mode: `verify`)
1. **Generate** — N diverse candidates from the free roster in parallel (reuse `live_parallel`). Distinct lineages, per [[cross-provider decorrelation]].
2. **Verify** — run each candidate against an **independent oracle** (see [[oracle independence]]), inside the [[execution sandbox]].
3. **Select** — keep the passers; rank by tests-passed, then tie-breakers. None pass → revise.
4. **Revise** — feed concrete failures (stderr / failing tests) back, bounded rounds. Critique-revise sits *on top of* selection because passing thin tests ≠ correct — the oracle bounds quality.

## Oracle independence (enforced, not optional)
The pass condition is **never** authored by the candidate being graded. Sources, in priority:
1. **User gold** — a test command (`--oracle "pytest -q"` / `/oracle`) or expected output.
2. **Ground truth** — compiles / runs / matches expected I/O.
3. **Test-author model** — a *separate* model writes tests **before** seeing candidates, ideally a different lineage.

### Oracle-ABSENT is a first-class, visible state
Most tasks ("write a function that does X") arrive with **no** gold. If there is no user gold **and** no independent test-author, the mode labels the result **"unverified — selection only"**. It must NEVER silently self-grade or fake a pass. No-oracle is a state, not an error.

## Execution sandbox (the dangerous piece — see [[execution sandbox]])
Candidates are untrusted, model-generated code. Tiers, auto-selected, active tier always reported:
- **docker** (if present): real isolation — `--network none`, mem/cpu/pids caps, non-root, only a work dir mounted. Network + filesystem isolated.
- **macos-seatbelt** (macOS, no Docker): `sandbox-exec` deny-network profile = host-level **no-net floor** + cpu rlimit. **Filesystem is NOT isolated** (runs as you).
- **subprocess** (fallback): temp cwd + scrubbed env + cpu rlimit. **NOT a security boundary** — no network block, no fs isolation. Reduced-isolation mode, stated loudly.

> macOS note: `sandbox-exec` is **deprecated** by Apple (works today). The no-net floor is **verified at startup** with a real network probe, never assumed — if it stops blocking (or sandbox-exec is gone), the sandbox **degrades to subprocess and warns** (`network_blocked=False`). A broken seatbelt fails as "no isolation, warned", never "network silently open while reported shut". (Confirmed by test.)

Honesty rule: never let "best-effort no-net" read as "no-net". rlimits cap CPU/mem only. Untrusted code with network can exfiltrate — surface the active tier, don't proceed silently. Real fs isolation = Docker.

## Oracle UX
- `--oracle "pytest -q"` — whole run.
- `/oracle <cmd>` — set during a session.
- No oracle → "unverified — selection only" (above), not an error.

## Modules
- `triad/sandbox.py` — `Sandbox.run(files, argv, timeout, mem) -> Result(returncode, stdout, stderr, timed_out, tier, network_blocked, fs_isolated, note)`. **Built first; independent of the gate.**
- `triad/oracle.py` — `Oracle.score(candidate) -> (passed, detail)`; Command / ExpectedOutput / TestAuthor impls. Built after the gate is green.
- `orchestrator.run_verify()` — wires the loop.

## Spend regimes (when to pay)
- full oracle → free generate-verify-select ([[free-cloud default]]).
- partial oracle (lint/types/schema) → free + **one** frontier review (single judge, not a council).
- no oracle → paid `council`.

## Open questions
1. Oracle UX details (above) — both `--oracle` and `/oracle`, no-oracle = unverified state.
2. Sandbox tech on a MacBook Air / no budget — lean: subprocess+seatbelt floor, auto-Docker when present; macOS fs-isolation gap stated honestly.

## Status
- [ ] gate (smoke_free.py) — proves the substrate before building on it.
- [ ] `sandbox.py` — built + tested against hand-written code (does not need the gate).
- [ ] `oracle.py` + `run_verify` — only after the gate is green.
