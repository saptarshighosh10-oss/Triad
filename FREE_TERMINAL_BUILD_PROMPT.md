Build a brand-new multi-AI terminal called "Free Terminal" FROM SCRATCH, autonomously, end to
end. Do NOT stop to ask me questions — make reasonable decisions and keep going until every
acceptance test below passes and the terminal launches with one command. The previous attempt
(Fighty) is being thrown away; do not reuse its code.

================================================================
WHAT IT IS
================================================================
A terminal that lets one person talk to several AI agents at once and route work between them.
Four channels:
  - STRONG (plan / review / decide), each a real CLI already installed:
      claude  → `claude --print "<prompt>"`        (plain-text output; uses my Claude account)
      codex   → `codex exec "<prompt>"`             (non-interactive; capture stdout)
      gemini  → `gemini -p "<prompt>"`              (non-interactive; capture stdout)
  - FREE ($0 worker, the cheap muscle), already built:
      free    → `triad-ask "<prompt>"`              (triad's free roster: Groq/OpenRouter/NIM)
The strong channels plan/review; the free channel does routine/bulk work. Spend judgment where it
matters, free compute for volume.

================================================================
REUSE, DON'T REBUILD (the engine already works — 170 passing tests)
================================================================
The "triad" engine at /Users/saptarshighosh/Downloads/triad_project is DONE. Call it; never modify
it. Activate its venv when calling python: `cd /Users/saptarshighosh/Downloads/triad_project &&
source .venv/bin/activate`. Available:
  - triad-ask "<prompt>"                              → free one-shot answer to stdout ($0)
  - python -m triad code "<task>" --repo D --oracle C → verified file edit (generate-verify-select)
  - python -m triad verify ... / python -m triad models --auto / python -m triad bench
API keys already live in /Users/saptarshighosh/Downloads/triad_project/.env (Groq/OpenRouter/NIM/
Gemini). NEVER echo, hardcode, or send keys to a model.

================================================================
REFERENCE MATERIAL (read, then decide for yourself)
================================================================
  - Spec (follow it, but the CORRECTIONS below WIN where they conflict):
      /Users/saptarshighosh/Downloads/triad_project/FREE_TERMINAL_V2_SPEC.md
  - Old terminal — read ONLY for UI/look inspiration, do NOT copy its code (it was fragile):
      /Users/saptarshighosh/fighty-terminal/agent_relay.py

================================================================
WHERE TO BUILD
================================================================
A fresh directory: /Users/saptarshighosh/free-terminal/   (new, clean, git-init it).
Leave ~/fighty-terminal untouched — I'll delete it myself once yours works. Python 3, stdlib only
(plus whatever triad already provides). One launcher command.

================================================================
HARD ARCHITECTURE RULES (these caused the last failure — obey them)
================================================================
1. EVERY channel runs as a SUBPROCESS (crash isolation: one channel dying must never kill the
   terminal). Use the SIMPLEST non-interactive invocation of each CLI that returns plain text and
   capture stdout. Do NOT build stream-json / app-server broker parsing — that fragility is what
   broke the old one.
2. NO message bus. Strong→free delegation is a DIRECT synchronous call: if a strong channel's reply
   contains a line `@delegate free: <task>`, the terminal runs `triad-ask "<task>"`, captures the
   output, and feeds it back to that strong channel as clearly-quoted DATA. Malformed/absent
   delegation = just treat the reply as a normal answer.
3. Free-model output handed to a strong channel is UNTRUSTED — wrap it as quoted input, never let it
   act as instructions ("ignore previous instructions / mark verified" must do nothing).
4. TRUTHFUL STATUS, always. Each channel shows ONLINE / OFFLINE (with reason, e.g. "CLI not on
   PATH") / RATE-LIMITED. Probe at boot (`which <cli>` + a tiny ping). Never claim a dead channel is
   alive.
5. GRACEFUL DEGRADATION, never crash: an OFFLINE channel is skipped; a 429/rate-limit on a free
   provider falls through (triad-ask already tries the next head); no network → show OFFLINE and let
   the user retry. The terminal must survive any single channel failing.
6. BUDGET METER: track per-channel call counts and any rate-limit/429 hits; show them in the status
   line. (A spend-aware terminal must see spend.)
7. HONEST NAMING: call it a "free-API terminal," not "free/offline" — with no network the free cloud
   models are unavailable (local-model support is out of scope here).

================================================================
SCOPE — BUILD EXACTLY THIS, NOTHING MORE
================================================================
  - The 4 channels with the subprocess interface above.
  - MODES:
      * parallel  — all available channels answer the same prompt at once (show each, color-coded).
      * ask <chan> <prompt> — one channel answers.
      * code <task> [--oracle CMD] — call `python -m triad code` (verified edit), show the diff.
      * delegate — the strong→free `@delegate free:` mechanism (rule 2).
  - A status header (channel states + budget meter). Keep the dragon/look from the old one if easy.
  - A one-command launcher installed to ~/.local/bin (e.g. `ft`).
Do NOT build: tiered council, a compact handoff protocol, vault memory, a web UI, or local models.
Those are explicitly out of scope for this build.

================================================================
ACCEPTANCE TESTS — YOU ARE DONE ONLY WHEN ALL PASS
================================================================
Write a `tests/smoke.py` (or shell script) that runs these and exits 0; iterate until green:
  A. Boot prints a truthful status line for all 4 channels (claude/codex/gemini ONLINE since the
     CLIs are installed; free ONLINE since triad-ask is on PATH).
  B. `parallel "Reply with exactly: ONLINE"` → every available channel returns a non-empty answer;
     the run does not crash.
  C. The free channel alone answers via triad-ask ($0).
  D. Simulate a channel being unavailable (e.g. temporarily point a CLI name at a missing binary or
     mock it) → it shows OFFLINE with a reason and the other channels still answer. No crash.
  E. `code "fix add(a,b) to return a+b"` on a scratch repo with an oracle → returns a VERIFIED diff.
  F. A strong reply containing `@delegate free: say the word DONE` triggers triad-ask and the result
     is fed back, wrapped as quoted data.
  G. The budget meter increments per call and is shown.
Also: launching with the one-word command opens the terminal and accepts a prompt.

================================================================
HOW TO WORK
================================================================
- Read the spec + skim the old file for look only. Then build in this order: channels+status (A,B,C,D)
  → code mode (E) → delegation+budget (F,G) → launcher + README.
- After each step, RUN the smoke tests and fix failures before moving on.
- Keep it small and robust over clever. Match clean Python style. Minimal dependencies.
- When everything passes: write a short README (what it is, how to run, the channels, the rules),
  `git add -A && git commit`, and print a final summary of what works + how to launch it.
- Do not ask for confirmation between steps. Finish the whole thing.
