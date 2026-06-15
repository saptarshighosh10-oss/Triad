# Architecture

Three independent tools that share a philosophy, not a codebase. This doc is the map so
you reach for the right one and don't expect one to do another's job.

## Which tool for which job

| You want to…                                          | Use                  | Cost   | Edits files? |
|-------------------------------------------------------|----------------------|--------|--------------|
| Compare how 3 frontier models answer the same thing   | `triad` (parallel)   | paid   | no           |
| Have models build on each other / synthesize          | `triad` (relay/council) | paid | no           |
| Pressure-test a real decision                         | `triad` (council)    | paid   | no           |
| Get actual coding work done for free                  | free-claude-code     | free   | **yes**      |
| A cheap second opinion without risking your repo      | TriAgent Sidecar     | cheap  | no           |

## How the pieces relate

```text
        ┌─────────────────────────────────────────────┐
        │  Layer 1: triad  (this repo)                │
        │  3 frontier models, paid APIs, chat-only    │
        │  parallel · relay · council                 │
        └─────────────────────────────────────────────┘
                 (no live link yet — see roadmap)
        ┌─────────────────────────────────────────────┐
        │  Layer 2: free-claude-code  (separate)      │
        │  proxy → free/local models drive Claude Code│
        │  per-tier routing, edits files, runs bash   │
        └─────────────────────────────────────────────┘
        ┌─────────────────────────────────────────────┐
        │  Layer 3: TriAgent Sidecar  (separate)      │
        │  cheap models = advisory only, no edits     │
        └─────────────────────────────────────────────┘
```

They're decoupled on purpose. Each is useful alone; nothing breaks if you only run one.

## triad internals (Layer 1)

```text
cli.py        REPL + slash commands + `setup` subcommand
  ├── dotenv.py     auto-load .env so keys work without `source`
  ├── keychain.py   optional OS-keychain storage (graceful no-op without `keyring`)
  ├── setup_keys.py masked-input wizard + live key validation
  ├── agents.py     OpenAIAgent / ClaudeAgent / GeminiAgent (async streaming, lazy SDKs)
  ├── orchestrator.py  parallel / relay / council modes
  ├── ui.py         rich Live: 3-column parallel + single-panel streaming
  └── skills.py     loads skills/*.md and applies them to agents' system prompts
```

The agent layer is the seam. To make `triad` itself run on free/local models later, you
add one `Agent` subclass that points at free-claude-code's proxy (or any OpenAI-compatible
endpoint) and register it in `build_agents()`. Modes, skills, UI, and the key wizard all
work unchanged. That's the bridge in the roadmap.

## Key handling (the part we just hardened)

Order of resolution at startup: real environment vars → `.env` (auto-loaded) → OS keychain
(for any still missing). `python -m triad setup` validates each key against the provider's
`/models` endpoint before saving — a dead key is caught at setup, not three turns into a task.
