# 2026-06-12 coder-context-slimming

Closed the last big single-shot token sink: the coder no longer ships the WHOLE repo to every head every round. Back to [[index]].

## What changed
`triad/coder.py` `build_context(task, files, max_bytes)`: if the repo fits the budget, include it all (small repos / the benchmark are unchanged). If not, **rank files by lexical relevance to the task** (reusing `memory._tokens`, same recall principle as the vault), include the most-relevant IN FULL up to the byte budget, and list the rest as a **name-only manifest** ("OTHER FILES … say which you need and I'll resend"). Files the task names by path are always included (big score boost). `_gen_prompt` uses it; `EditJob.run` reports the slim once per run.

## Verified
- 163/163 offline tests (+5: small-repo-all-included, relevant-file-in-full, others-dropped-to-manifest, omitted-still-listed, respects-budget).
- **LIVE (free roster, tiny budget to force the path):** repo = 1 relevant buggy file + 4 filler files → "context: sent **1/5 files by relevance (~48 tok)**; 4 listed by name only"; both heads passed, VERIFIED, diff fixed `a-b`→`a+b`. Editing still correct with the slimmed context.

## Why
This is the recall-over-re-read principle applied to the coder, for the same capped-tier/budget audience: a model editing a real repo no longer pays for the whole tree each round, just the files the task is about (+ it still sees the full file *map*). Honest limit: lexical relevance can miss a needed file that doesn't share task vocabulary — the model can ask for it by name (manifest is shown), and a future embedding tier would tighten this.

## Status
- 163/163 tests, live-verified. Reproduce: a repo > `max_context_bytes` triggers it; the run prints the slim line.
- Open: embedding-tier relevance (opt-in); multi-seed pass-rate + NIM 3rd head; wire `/api/code` into the web UI.

Builds on [[2026-06-12 stage-3-engine-built]] and [[2026-06-12 token-savings-and-evict]].
