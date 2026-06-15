# stage 3 — fcc file-editing bridge

Scope for the last unchecked roadmap item: make triad **drive [[layer 2 free-claude-code]]'s real file-editing agent**, not just chat to it. Saved before the build so it survives a cutoff. Back to [[index]].

## Why this is the load-bearing item
Today Layer 1 (triad, paid orchestration) and Layer 2 (free-claude-code, free coding) **do not connect**. Two bridges exist in principle; only the cheap one is wired:

- **Chat bridge — already supported, trivial.** `TRIAD_CLAUDE_BASE_URL=http://localhost:8082` points triad's Claude *slot* at the fcc proxy. fcc speaks the Anthropic API, so triad's Claude agent talks to a free model with zero new code. This is **chat only** — it cannot edit files.
- **File-editing bridge — the missing piece (this doc).** triad invokes fcc's actual coding agent so a free model **edits real files**, on a tree you can throw away.

## What fcc actually is (grounded, verified locally)
- `fcc-server` → an **Anthropic-API-shaped proxy** on `0.0.0.0:8082` (`PORT=8082`). It routes the Anthropic tiers (`MODEL_OPUS` / `MODEL_SONNET` / `MODEL_HAIKU` / `MODEL` fallback) to free providers (NIM / Groq / OpenRouter / local), per `~/.fcc/.env`.
- `fcc-claude` (`cli.entrypoints.launch_claude`) → finds the real `claude` CLI (`shutil.which`), strips `ANTHROPIC_API_KEY`, sets `ANTHROPIC_BASE_URL = <fcc proxy>`, and `subprocess.Popen([claude, *args], env=...)`. **So the file-editing agent is Claude Code itself, running on free models.**

> Implication: the bridge does **not** call an SDK. It spawns a **CLI** (`claude`, headless) with two env vars. The hard parts are isolation, gating, and apply/discard — not the call.

## The integration shape
triad gets a new mode/command that runs an **agentic edit job**:

1. **Isolate** — never touch the user's live tree. Create a throwaway workspace from the target repo:
   - primary: `git worktree add <tmp> HEAD` (clean checkout of current commit), or a `git clone --local` for full separation.
   - the cheapest models get write+bash here only because the whole tree is one `git worktree remove` / `git reset --hard` away. (PLAN.md decision: *don't hand the cheapest models write+bash on anything you can't `git reset`*.)
2. **Drive** — run Claude Code **headless** in that workspace against the fcc proxy:
   - `claude -p "<task>" --output-format stream-json --permission-mode acceptEdits` (or `--dangerously-skip-permissions` *inside the disposable tree only*), with `cwd=<workspace>`, `env` = current env minus `ANTHROPIC_API_KEY` plus `ANTHROPIC_BASE_URL=http://127.0.0.1:8082`, `ANTHROPIC_MODEL` selecting the tier.
   - stream the JSON events to the console (and the web UI) so edits are visible live.
3. **Gate** — when the agent stops, compute `git diff` in the workspace and **verify** it: reuse the verify-mode [[generate-verify-select]] machinery — run the user's `--oracle` (e.g. `pytest -q`) against the *edited tree*. No oracle → **"unverified — review the diff"**, never an implied pass ([[oracle independence]] applies unchanged).
4. **Apply or discard** — show the diff + gate result. On accept: `git apply`/merge the patch back into the real tree (or fast-forward the worktree). On reject: remove the worktree, real tree untouched.

## Isolation model (the dangerous piece — see [[execution sandbox]])
Two different untrusted surfaces, don't conflate them:
- **The agent's file writes** → bounded by the **disposable git worktree** (filesystem blast radius = the throwaway tree; recovered with one git command). This is the *primary* control for edits.
- **Code the agent executes** (its bash tool, running tests) → bounded by the existing `Sandbox` tiers (docker → seatbelt no-net floor → subprocess). Reuse [[execution sandbox]] as-is for the oracle/test run; surface the active tier.
- macOS honesty rule carries over: seatbelt blocks network but **not** filesystem — so the worktree, not the sandbox, is what protects your other files from the agent's edits. State this loudly.

## Preconditions / health checks (fail fast, never silently)
- `claude` on PATH (`shutil.which`) — else: "install `@anthropic-ai/claude-code`".
- fcc proxy reachable on `:8082` — probe before launching; offer to start it (`fcc-server`) or print the command.
- target dir is a clean git repo (no uncommitted changes) — else refuse or stash, so "discard" is truthful.

## UX / commands (mirror existing triad surface)
- CLI: `python -m triad code "<task>" [--oracle "pytest -q"] [--repo .] [--tier sonnet]`.
- REPL: `/code <task>` (runs the edit job), `/oracle` reused for the gate, `/apply` / `/discard` after review.
- Web UI: a "Code" panel — task box + live stream + diff view + Apply/Discard buttons. (Separate from the chat modes; this is an edit job, like verify is a pipeline not a chat.)

## Modules to add (keep the terminal + web untouched)
- `triad/fcc.py` — `proxy_health()`, `claude_bin()`, `child_env(tier)` (the two-env-var setup), `start_proxy()` helper.
- `triad/coder.py` — `EditJob`: make workspace → run `claude -p` (stream) → `git diff` → gate via oracle/sandbox → `Result(diff, verified, detail, workspace)`; `apply()` / `discard()`.
- Wiring: `code` subcommand in `cli.py`; `/code` in the REPL; a `/api/code` route + diff panel in `web.py`.

## Convergence with agent-relay-terminal
This is the same primitive the relay terminal already uses — **driving a coding CLI as a subprocess** (it orchestrates codex/claude/gemini CLIs locally over `subprocess`). Decision to make at build time: put `EditJob` in triad and let the relay terminal call it, or host the subprocess-driver in the relay and have triad invoke the relay. Lean: **`EditJob` lives in triad** (it owns the oracle + sandbox gate); the relay terminal calls it for its "claude" lane. One driver, two front-ends.

## Spend regime (unchanged from [[free-cloud default]])
Full oracle → free edit + free gate ($0). Partial oracle (lint/types) → free edit + **one** frontier review of the diff. No oracle → free edit, **diff returned unverified** for human review (never auto-applied).

## Open questions (decide at build, log conservatively like verify did)
1. **headless flags** — `claude -p` exact flags for non-interactive edits + machine-readable result; confirm `stream-json` event shape and the stop signal.
2. **permission mode** — `acceptEdits` vs `--dangerously-skip-permissions`; the latter only ever inside the disposable worktree, never the live repo.
3. **apply strategy** — patch back (`git diff > p; git apply p`) vs promote the worktree; how to handle conflicts if the live tree moved.
4. **tier → model** — does `ANTHROPIC_MODEL` reach fcc's router, or must the tier be chosen by which `claude` model id is requested? Verify against fcc's routing.
5. **proxy lifecycle** — triad auto-start `fcc-server` (and own its lifetime) vs require it already running.

## Status
**v1 BUILT via a different (better) path — see [[2026-06-12 stage-3-engine-built]].** v1 skipped git worktrees + the headless `claude` CLI in favour of **in-memory whole-file edits** gated by the existing sandbox: more reliable for free models, zero blast radius until accept, no fcc dependency.
- [x] `coder.py` — `EditJob`: read repo → 3 heads emit whole-file edits → apply in-memory → gate via `oracle.check_workspace` in the sandbox → select → critique-revise → diff → `apply_edits`.
- [x] `CommandOracle.check_workspace` — multi-file gate (independence unchanged).
- [x] wire `code` subcommand + REPL `/code`. (`/api/code` web panel deferred — user driving web UI separately.)
- [x] live smoke: buggy `add(a,b)` on a scratch repo, free roster, gated by an oracle → VERIFIED + applied.
- [x] 126/126 offline tests (+21 coder: parse/diff/path-guard/dotfile-skip/engine pass·revise·fail·no-oracle).
- [ ] **3c benchmark** (the college deliverable): single-model vs three-head verify-select pass-rate, X% vs Y%.
- [ ] `fcc.py` + headless-`claude`-via-fcc — deferred for complex multi-step / large-repo tasks the whole-file format can't express.
