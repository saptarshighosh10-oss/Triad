# The Plan

One accessible multi-agent setup: use strong models where judgment matters, cheap or
free models where it doesn't, skills as reusable recipes, and keep dangerous
permissions away from unreliable models. Built so someone with **no budget** can still
get real work done.

---

## Three layers — they do different jobs, don't conflate them

### Layer 1 — Orchestrator: `triad` (this repo)
- Talk to ChatGPT + Claude + Gemini at once: `parallel` / `relay` / `council`.
- Uses the **paid** OpenAI / Anthropic / Gemini APIs directly. **Costs money per message.**
- Use it for: comparing frontier takes, collaboration between strong models, and the
  council pattern for high-stakes decisions.

### Layer 2 — Cheap execution: free-claude-code (separate install)
- A proxy that points **Claude Code** (a real file-editing coding agent) at free or local
  models, routed per tier. Repo: `github.com/Alishahryar1/free-claude-code`.
- **Free**, because the target user has no budget. Local is optional for those with the RAM.
- Use it for: actual coding work at $0.
- Recommended per-tier config lives in [`config/free-claude-code.env`](config/free-claude-code.env).

### Layer 3 — Advisory sidecar: TriAgent Sidecar (separate, your build)
- Cheap models as **advisory-only** reviewers. They do **not** edit files or make final calls.
- Use it for: a cheap second opinion / code review / error summary without risking your repo.

> The big one: **Layer 1 is paid, Layer 2 is free, and they don't connect today.** Running
> `triad` bills your API keys; it is *not* the free-model setup. Picking the right layer for
> the job is the whole point.

---

## The decision (recorded so it doesn't get lost)

From the council, after correcting for the real constraint (a **MacBook Air**, and users
who can't pay):

- **Free-cloud by default.** "Just run it locally" is the privilege answer — the target
  users don't have the hardware. Free cloud routing is the *point*, not the compromise.
- **Local is an opt-in** for whoever has the RAM. free-claude-code already ships Ollama /
  LM Studio / llama.cpp — it's one config entry, no extra code.
- **Per-tier routing is about spreading rate limits, not cost** (cost is already $0).
  Capable model on Opus/Sonnet tiers, fast model on Haiku/subagent tier, a *different*
  provider as fallback so one 429 doesn't kill a session.
- **Validate every model slug** in the Admin UI before trusting it — free catalogs drift.
- **Don't hand the cheapest models write + bash on anything you can't `git reset`.** Run
  agentic edits on a clean tree so a bad run is one command away from undone.
- The "free tiers may train on your inputs" privacy caveat only bites on **sensitive data**,
  which is out of scope for this project now.

---

## Roadmap

- [x] `triad` orchestrator (parallel / relay / council) + skill files
- [x] API key wizard: masked prompts, live validation, safe save, `/keys`, keychain option
- [x] **Free roster (Stage 1):** run triad on free models — `--free` (Groq / OpenRouter / NIM,
      deliberately distinct lineages) and a free Claude slot via `TRIAD_CLAUDE_BASE_URL` →
      free-claude-code (it speaks the Anthropic API, so no new agent code).
- [ ] **Next — the load-bearing wall: generate-verify-select.** An executable oracle (run the
      tests/code) that *selects* the candidate that passes and feeds failures back to revise.
      Without it, free aggregation is the correlated-agreement trap; the free roster above is only
      substrate. The oracle bounds quality, so a critique-revise loop sits on top of selection.
      Honest scope: this is a force multiplier on **verifiable** tasks (code/math/structured),
      not subjective ones — where paid `council` stays the right tool.
- [ ] Drive free-claude-code's file-editing agent (subprocess, sandboxed on a clean `git` tree
      so a bad run is one `reset` away) — a *separate* integration from the chat bridge above,
      and where this converges with the agent-relay-terminal work.
