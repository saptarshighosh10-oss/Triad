# Free Terminal v2 — Build Spec

A clean restart of the multi-AI terminal. v1 (Fighty / agent_relay) is being retired as the
front-end; the **triad engine is kept** (it works — 170 passing tests). v2 is a thin, robust
orchestrator over that engine that finally connects the two halves we kept separate:
**strong agents that plan/decide/review** and **free models that do the volume**, with an
**executable oracle** making the free output trustworthy.

> Reviewer: check this for (a) internal consistency, (b) feasibility for a solo teen builder,
> (c) anything under-specified or contradictory, (d) whether it actually fixes v1's failures.
> Specs are numbered for reference.

---

## 1. Why restart (what v1 got wrong)
- **F1. CLI-fragility.** Fighty drove `codex`/`claude`/`gemini` as subprocess CLIs. The `claude`
  CLI silently broke (corrupt npm install, postinstall skipped by pnpm → "OFFLINE — CLI not on
  PATH"). A whole channel died with no fallback.
- **F2. No free integration.** Fighty used paid/account CLIs only; the free roster (Groq/OpenRouter/
  NIM) lived in triad and was never wired in. The "free terminal" wasn't actually free.
- **F3. Two disconnected halves.** triad (free APIs, verify-select, memory) and Fighty
  (orchestration, message bus, UI) never talked. The orchestration couldn't command the free muscle.
- **F4. No graceful degradation.** One offline channel, a rate-limit (Groq daily cap), or no network
  took things down instead of falling back.

## 2. Thesis & audience (unchanged, now load-bearing)
- **A1.** Target = budget-constrained users on BOTH ends: **$0/no-budget** AND **paying-but-capped**
  (lower tiers hitting context/token/rate limits). One toolkit serves both.
- **A2.** Spend judgment where it matters, free compute for volume. An **oracle** is what lets you
  trust cheap output, so the oracle decides the architecture and when to pay.
- **A3.** Everything stays **local-key, human-readable, honest** (never report safer/cheaper/more
  isolated than it really is).

## 3. Architecture (one orchestrator, tiered roster, shared engine)
```
                 ┌─────────────── Free Terminal v2 (orchestrator + UI) ───────────────┐
   user prompt → │  router (spend-regime) → roster channels → verify-select → memory  │
                 └────────────────────────────────────────────────────────────────────┘
   STRONG channels (plan / review / decide)        FREE worker channels (generate / execute)
   ├ claude  (account, or free via fcc :8082)       ├ groq        (Llama-3.3-70b)
   ├ codex   (OpenAI account)                        ├ openrouter  (gpt-oss-120b)
   └ gemini  (free tier)                             └ nim         (MiniMax-M3)   [distinct lineages]
                         │                                            │
                         └──────────── shared triad engine ───────────┘
        oracle · sandbox(docker/seatbelt/subprocess + node) · coder(EditJob) · vault memory ·
        compact protocol · skills · bench · catalog(model auto-validate)
```
- **S1.** v2 is a **front-end + router** only. It does NOT reimplement the engine — it calls triad
  (in-process for free channels; as CLIs for strong channels). Single source of truth.
- **S2. Channels are uniform.** Every channel — strong or free — exposes the same interface:
  `answer(prompt, system?) -> text` and (where capable) `edit(task, repo, oracle) -> diff`.
  Strong = subprocess CLI; free = triad agent. The router doesn't care which.
- **S3. The free worker is "just give it the prompt."** Free channels are reached via
  `triad ask "<prompt>" [--skill X]` (one-shot, stdout) or `triad code "<task>" --oracle ...`
  (verified edit). Already built. A strong agent commanding the free muscle = handing it that prompt.

## 4. Roster & decorrelation
- **R1.** Free roster MUST be **distinct lineages** (Groq=Llama / OpenRouter=GPT-OSS / NIM=MiniMax);
  pseudo-diversity (three finetunes of one base) defeats verify-select. Gemini free tier may be a
  4th. (Empirically: on easy tasks they all hit ~100%, so decorrelation only pays on HARD tasks.)
- **R2.** Strong roster = `claude` (account, or pointed at fcc `ANTHROPIC_BASE_URL=:8082` to run
  free), `codex`, `gemini`. Roles: a planner, a reviewer, a searcher (configurable).
- **R3. Model slugs drift.** On startup (and on demand) run `triad models --auto`: query each
  provider's live `/models`, validate the configured slug, auto-pick a valid default, write `.env`.
  Never trust a hardcoded slug.

## 5. Modes (all exist in the engine; v2 surfaces them)
- **M1. parallel** — every channel answers at once.
- **M2. relay** — channels answer in sequence, each seeing the prior (compact handoff, §8).
- **M3. council** — all answer, a chair synthesizes. **Tiered:** free channels = the 5 advisors,
  ONE strong channel = the chair. (~90% of tokens go free; the load-bearing judgment stays sharp.)
- **M4. verify** — generate-verify-select: N free candidates → oracle → select → critique-revise.
- **M5. code** — the EditJob (whole-file edits, in-memory, sandbox-gated, apply/discard).
- **M6. ask** — one-shot free answer (the worker primitive).
- **M7. bench** — pass-rate (single vs three-head) + token-savings + multi-seed variance.

## 6. Verify-select (the trust mechanism) — KEEP AS BUILT
- **V1.** `Oracle.check` / `check_workspace`: run the USER's command/tests against candidate code in
  the sandbox. Pass iff exit 0.
- **V2. Oracle independence (enforced by construction):** the pass condition is never authored by the
  candidate being graded. Sources: user gold > ground-truth execution > separate test-author model.
- **V3. Oracle-ABSENT is a first-class visible state:** no oracle → "unverified — selection only",
  NEVER a fabricated pass, NEVER self-grading.
- **V4. Loop:** generate N diverse → verify each in sandbox → select passers → feed concrete failures
  back, bounded rounds (default 3). Honest "0/N passed" if none.

## 7. Sandbox (isolation) — KEEP AS BUILT
- **X1. Tiers, strongest first, ALWAYS reported:** docker (net+fs isolated) → macos-seatbelt
  (net blocked, **fs NOT isolated**) → subprocess (no boundary, stated loudly).
- **X2. Verified, not assumed:** seatbelt's no-net floor is exercised with a real probe at startup;
  if it stops blocking, **degrade to subprocess and warn** — never "network silently open while
  reported shut".
- **X3. JS/TS support:** the dir of detected runtimes (`node`/`deno`/`bun`) is added to the locked
  PATH (only the interpreter dir, net still blocked) so oracles can run TS via
  `node --experimental-strip-types` (zero-compile). Already built + live-verified on a real TS repo.
- **X4.** rlimits cap CPU/mem only — never sold as network/fs protection.

## 8. Memory — recall instead of re-read — KEEP AS BUILT
- **MEM1.** Persist the session to an Obsidian-compatible vault (notes per agent/topic/checkpoint).
- **MEM2.** Each turn, **recall** only the relevant notes (lexical TF-IDF, NOT embeddings — embeddings
  cost a call/turn and defeat the budget goal). Inject as ephemeral context, never stored (no
  compounding). ~96% fewer tokens vs re-sending the vault (live-measured).
- **MEM3.** Bound per-agent history to the last N exchanges; **summarize-on-evict** writes trimmed
  turns to the vault so they stay recallable. Bounded history is lossless against the vault.

## 9. Compact protocol (squished handoff) — KEEP AS BUILT
- **P1.** In relay/council, agents emit a terse schema (`@goal/@find/@conf/@next`, "be dense");
  only the digest is passed forward; full output lives in a RefStore (`ref:key`).
- **P2.** It is a compact SCHEMA the models already know + reference-passing — **NOT a novel
  compressed language** (that pushes models out-of-distribution and kills human oversight). Report
  the tokens saved; the saving grows with conversation length.

## 10. Spend-regime router (the orchestration brain)
- **SR1.** Full oracle available → free generate + free gate ($0).
- **SR2.** Partial oracle (lint/types/schema) → free generate + **one** strong-channel review.
- **SR3.** No oracle, high stakes → strong `council` (free advisors + strong chair).
- **SR4. Strong commands free:** a strong channel can emit a task addressed to the free worker
  (`to: free`); the router runs it via `triad ask`/`triad code`, gates it, returns the result for the
  strong channel to review. This is "Fighty tells the free stuff what to do," concretely.
- **SR5. Degrade gracefully (fixes F1/F4):** any channel can be OFFLINE; the roster routes around it.
  A 429 (e.g. Groq daily cap) → try the next free head. No network → free models unavailable →
  fall back to whatever strong channel is up, or to cached/scripted output. NEVER hard-fail.

## 11. Skills (model-agnostic) — KEEP AS BUILT
- **SK1.** Skills are markdown (frontmatter + body) applied as a system prompt — work on ANY model.
- **SK2.** `triad ask --skill X` and channel configs can wear a skill (e.g. a planner skill on the
  strong chair, a worker skill on the free roster, the `crewmate` persona for the game).

## 12. UI (terminal first, web optional)
- **UI1. Terminal:** channel status line (each channel ONLINE/OFFLINE/RATE-LIMITED with reason),
  the dragon sentinel, per-channel colored streaming, mode switch. Keep v1's look; fix its guts.
- **UI2. Status must be truthful** (the v1 "OFFLINE — CLI not on PATH" was actually correct and
  useful — keep that honesty; just add fallback so offline ≠ dead).
- **UI3. Web (optional):** reuse triad's `web.py` (stdlib http+SSE, one persistent loop) for a
  browser front-end; not required for v1 of the terminal.

## 13. Health & preconditions (fail loud, not silent) — fixes F1
- **H1.** On boot, probe every channel: free = a 1-token ping or `/models` reachability; strong =
  `which <cli>` + `--version`. Show real status; never assume.
- **H2.** If a strong CLI is missing, say exactly how to fix it (e.g. the claude reinstall:
  `npm install -g @anthropic-ai/claude-code --include=optional`, run postinstall under npm not pnpm).
- **H3.** fcc proxy (for free-claude) probed on :8082; offer to start `fcc-server` or print the cmd.

## 14. Security / honesty invariants (non-negotiable)
- **SEC1.** API keys live only in `~/.fcc/.env` / triad `.env` (gitignored, mode 600), never in the
  browser, never echoed, never sent to a model. (A pasted key in chat = rotate it.)
- **SEC2.** Untrusted (model-generated) code only runs in the sandbox; the active tier and its gaps
  ride on every result.
- **SEC3.** Free edits never auto-applied without either an oracle pass or explicit human review.
- **SEC4.** Honest reporting everywhere: a skipped step is stated, a fallback is stated, an
  unverified result is labelled unverified.

## 15. Build phases (smallest shippable first)
- **B1. Core loop (week 1):** orchestrator + uniform channel interface + free roster via `triad ask`
  + truthful status line + graceful offline. Replaces Fighty's guts. Ship `parallel` + `ask`.
- **B2. Tiering:** spend-regime router + `relay`/`council` (free advisors + strong chair) + compact
  protocol. Strong-commands-free (SR4).
- **B3. Verified work:** wire `verify` + `code` (already built) as terminal modes with the oracle.
- **B4. Memory:** vault recall + bounded history into the loop.
- **B5. Polish:** model auto-validate on boot, dragon UI, web front-end (optional), bench command.

## 16. Open questions for the reviewer
1. **In-process vs subprocess for free channels?** Calling triad in-process is faster but couples the
   terminal to triad's Python; calling `triad ask` as a subprocess is uniform with the strong CLIs
   but slower. Spec leans subprocess for uniformity (S2) — is that the right call?
2. **Strong-commands-free routing (SR4):** message-bus (inbox/outbox like v1) vs direct synchronous
   call? v1's bus added complexity that contributed to F3. Lean: direct call, no bus.
3. **Council tiering economics:** is "free advisors + strong chair" actually cheaper enough to matter
   vs just running the strong council, given strong chair still reads all free advisor output?
4. **Offline story:** with no network, free cloud models are dead. Is local-model (Ollama) support a
   v1 requirement for a true "free/offline terminal," or a later opt-in?
5. **Scope realism:** is B1–B5 doable solo, or should v1 be ONLY B1 + B3 (free roster + verified
   work) and drop tiering/memory to later?
```
```

_Spec authored to be checked by a second AI before building. Engine status: built (170 tests).
Terminal status: to rebuild per the above._
