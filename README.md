# triad

Talk to **ChatGPT, Claude, and Gemini at the same time** from your terminal.
Fire all three off at once, watch them stream side by side, broadcast one task to
all of them, hand them roles via skill files, and have them collaborate.

```
triad[parallel]› design a rate limiter for a REST API

╭──── ChatGPT ────╮  ╭──── Claude ────╮  ╭──── Gemini ────╮
│ token bucket... │  │ I'd use a slid │  │ Here are three │
│ ✓ done          │  │ ● thinking…    │  │ ● thinking…    │
╰─────────────────╯  ╰────────────────╯  ╰────────────────╯
```

> **Scope:** this repo is **Layer 1** of a bigger plan — the paid orchestrator. It calls
> the OpenAI/Anthropic/Gemini APIs directly, so it **costs money per message**. The free
> coding-agent layer (free-claude-code) and the advisory sidecar are separate tools. The
> whole plan and how the layers fit live in **[PLAN.md](PLAN.md)** and
> **[ARCHITECTURE.md](ARCHITECTURE.md)**; the free routing config is in
> **[config/](config/free-claude-code.env)**.

## Install

```bash
cd triad_project
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # rich + the three provider SDKs
```

You only need the SDKs/keys for the agents you actually want. Missing keys just
disable that agent — the rest still run.

## Configure

Run the setup wizard — masked prompts, live key validation, saves safely:

```bash
python -m triad setup            # OpenAI / Anthropic / Gemini
python -m triad setup --all      # also NVIDIA NIM / Groq / OpenRouter
python -m triad setup --reconfigure   # re-prompt keys already set
```

It validates each key against the provider's `/models` endpoint (no tokens spent),
writes `.env` with `600` perms, and adds `.env` to `.gitignore`. If the `keyring`
package is installed it offers to store keys in your OS keychain instead. Keys are
auto-loaded on startup, so you never need to `source` anything.

You can also add or fix a key mid-session with `/keys`, or set the variables
manually — see `.env.example`. Keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY`. Models are env-overridable (`TRIAD_OPENAI_MODEL`, etc.) since
provider model IDs change constantly.

## Run

```bash
python -m triad                 # parallel mode
python -m triad --mode council
python -m triad --mode relay --chair claude
```

Type a message → it's broadcast to all active agents in the current mode.
Slash commands control everything else.

## Free models (no budget)

Run the whole thing on **free** models instead of the paid trio:

```bash
python -m triad --free            # roster = Groq + OpenRouter + NIM (3 different lineages)
python -m triad --roster all      # paid + free together
python -m triad setup --all       # configure the free providers' keys
```

The free defaults span three model **lineages** on purpose (Llama / GPT-OSS / Qwen) —
three finetunes of one base make *correlated* errors, which makes aggregation worthless.
Override per provider with `TRIAD_GROQ_MODEL` / `TRIAD_OPENROUTER_MODEL` / `TRIAD_NIM_MODEL`
(free catalogs drift — validate any slug before trusting it).

> **Free models need a verifier.** Three free chat opinions tend to make correlated errors
> and then agree. The free roster is *substrate*: trust it on **verifiable** tasks (run the
> code/tests) paired with selection — not on subjective ones. Paid `council` stays the right
> tool for high-stakes calls with no oracle to check against.

### Run the Claude slot on a free model (via free-claude-code)

free-claude-code speaks the Anthropic API, so point triad's Claude at it — no code change:

```bash
TRIAD_CLAUDE_BASE_URL=http://localhost:8082 python -m triad
```

`TRIAD_CLAUDE_MODEL` then just selects the fcc *tier* (e.g. `claude-sonnet-4-6` → fcc's
`MODEL_SONNET`); the actual free model is set in fcc's own config. (Scoped to triad on
purpose — a global `ANTHROPIC_BASE_URL` would hijack every Anthropic client you run.)

## Modes

| mode       | what happens |
|------------|--------------|
| `parallel` | Same task to all three at once. Independent answers, side by side. Best for comparing takes. |
| `relay`    | Agents work **in sequence**, each building on the running transcript. Best for plan -> build -> review pipelines. |
| `council`  | Everyone answers independently, then a **chair** agent synthesizes the single best answer. Best for hard decisions. |

## Commands

```
/mode parallel|relay|council     switch collaboration mode
/protocol on|off                 compact handoffs in relay/council (saves tokens)
/skill <name> [agent|all]        apply a skill (default target = its frontmatter)
/skills                          list available skill files
/clearskills                     strip all applied skills
/agents                          list active agents + models
/keys                            add or fix an API key (re-runs setup)
/reset                           clear conversation history
/save [file.md]                  save transcript
/help                            command help
/quit                            exit
```

## Skill files

Skills are markdown files in `skills/` with a tiny frontmatter block. Applying one
appends its body to that agent's system prompt, so you can stack several:

```markdown
---
name: code-reviewer
description: Hunts for bugs, edge cases, and security issues
agents: [gemini]        # default target; "all" = everyone
---
You are a rigorous code reviewer. Do NOT rewrite the whole thing...
```

Drop in as many `.md` files as you want — they're auto-discovered at startup.
Seven are included (planner, architect, implementer, code-reviewer, red-team,
synthesizer, concise).

A natural pipeline:

```
/mode relay
/skill planner all
/skill implementer chatgpt
/skill code-reviewer gemini
build me a CLI todo app in python
```

ChatGPT plans + implements, the transcript flows to the next agent, Gemini reviews
what came before.

## Obsidian vault (live knowledge graph)

Persist a session as a folder of linked markdown notes that opens straight into Obsidian's
graph view — one living note per agent, seeded topic notes, and an `index.md` hub:

```bash
python -m triad --vault triad_vault          # write live notes to triad_vault/
python -m triad --seed-file triad_vault      # resume: load triad_vault/index.md as context
```

Notes update after every turn (or run `/remember` to flush now). The vault is seeded with the
project's key topics already cross-linked (`[[generate-verify-select]]`, `[[oracle independence]]`,
`[[execution sandbox]]`, `[[cross-provider decorrelation]]`, `[[free-cloud default]]`, the three
layers), so the graph is populated on first open — not three lonely nodes.

**Open it in Obsidian:**
1. Install Obsidian (https://obsidian.md).
2. *Open folder as vault* → pick the `triad_vault/` folder.
3. Open the graph view (**Cmd/Ctrl+G**).

Obsidian watches the folder live, so new notes and links appear within a second while a session
runs. Wikilinks resolve by note name, so `[[free-cloud default]]` is an edge to that note.

## Project layout

```
triad/
  config.py        model IDs + key env mapping (all env-overridable)
  agents.py        Agent base + OpenAI / Claude / Gemini (async streaming, lazy SDK import)
  skills.py        skill-file loader (no PyYAML dependency)
  ui.py            rich Live: 3-column parallel streaming + single-panel streaming
  orchestrator.py  parallel / relay / council modes
  cli.py           the REPL + slash commands
skills/            the seven example skill files
```

## Adding a fourth provider

Subclass `Agent` in `agents.py`, set `name`/`label`/`color`, implement the async
`_provider_stream(self, messages, system)` generator, add a `key_env`/`model`
entry to `config.py`, and add the class to `build_agents()`. That's it — modes,
skills, and the UI work automatically.

## Notes

- Streaming is genuinely concurrent (asyncio): you'll see whichever model is
  fastest finish first while the others keep going.
- The Gemini SDK surface shifts between versions; `agents.py` handles both the
  awaitable and direct-async-iterator forms of the streaming call. If Google
  changes it again, that one method is where to look.
- Want a fuller scrolling TUI later? The `ui.py` layer is the only thing to swap
  (e.g. for `textual`); everything else stays.
