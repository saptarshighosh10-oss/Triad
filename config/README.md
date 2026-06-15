# free-claude-code config notes

`free-claude-code.env` here is a **reference**, not a file free-claude-code reads. fcc
manages its own settings through its Admin UI, so you apply these values there.

## Apply it

1. Install and start free-claude-code: `fcc-server`.
2. Open the Admin UI URL it prints (e.g. `http://127.0.0.1:8082/admin`, loopback-only).
3. **Providers** view: paste `NVIDIA_NIM_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`,
   then **Validate** each.
4. Set `MODEL_OPUS`, `MODEL_SONNET`, `MODEL_HAIKU`, and the fallback `MODEL` to the slugs
   in `free-claude-code.env`. **Validate** each slug — if one 404s, the catalog moved;
   swap it for whatever that provider currently lists. Take the *structure* (capable /
   fast / fallback) as fixed, not my exact strings.
5. **Apply.** Restart the server if it asks.
6. Run `fcc-claude` to launch Claude Code through the proxy.

## The one test that matters

Before trusting any of this, point fcc at a **single local Ollama model** (or one free
provider) and have it complete one real multi-step task on a **throwaway git repo**.
Watch whether it finishes a tool chain without flailing or leaving a half-edited mess.
That single result tells you if free-model *execution* is good enough to rely on. Only
then expand to per-tier routing and your real repos.

## Why fallback matters

Free tiers throw 429s mid-task. With a coding agent that has write access, an aborted run
can leave a repo half-edited — worse than no run. A fallback `MODEL` on a *different*
provider means a NIM limit rolls over to OpenRouter instead of stopping cold. Always work
on a clean tree so a bad run is one `git reset --hard` away.
