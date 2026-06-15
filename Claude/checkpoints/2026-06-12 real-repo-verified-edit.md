# 2026-06-12 real-repo-verified-edit

The dragon made a **verified edit on a real TypeScript project** — better-schoology (the testing copy) — by free models, gated by an executable test, $0. Full arc working end to end. Back to [[index]].

## What happened
- **Enabler — sandbox JS/TS support:** `sandbox.py` now adds the dir of detected runtimes (`node`/`deno`/`bun`) to the otherwise-locked sandbox PATH (`_extra_bin_dirs`), so JS/TS oracles run. Only the interpreter's dir is added (not the whole host PATH); network stays blocked by the tier. Uses **Node 22 native type-stripping** (`node --experimental-strip-types`) — runs `.ts` with zero compilation, zero new deps. (+2 tests; 170/170.)
- **Target:** `extension/lib/transform.ts :: parseGrade` — pure, no imports, ideal oracle seam.
- **Independent oracle (authored by me, not the models):** a TS test asserting existing behavior + a real gap — bare percentages ("97.44%" → percent extracted) and whitespace-padded grades, which the old code lost.
- **Result:** Groq **429'd** (daily token cap) but the run survived — **OpenRouter + NIM both passed** the oracle under the network-blocked seatbelt sandbox; VERIFIED, applied to the testing copy. parseGrade now `.trim()`s and handles bare `NN%`, existing cases preserved.
- **Context-slimming on a real tree:** "sent 13/28 files by relevance (~29,794 tok); 15 by name only" — didn't dump the whole `lib/`.

## Why it matters
This is the thesis on real code, not toys: free models + executable oracle + sandbox → a trustworthy, applied improvement to a real repo. And Groq's 429 was a live demonstration of the multi-provider rate-limit-resilience argument (one provider died, two carried).

## Guardrails honored
Testing copy only (`TESTING_COPY.md`); edit is school-agnostic (a pure parser); change is `git`-tracked (` M extension/lib/transform.ts`) so it's trivially reviewable/revertible; NOT pushed anywhere.

## Open / next
- Generalize: JS/TS oracle now works for any node project; docker-tier would need node in the image (documented).
- Pick more better-schoology targets with clean import graphs (the scrapers/transforms) for more verified contributions.
- The verified-edit-on-real-repo path is the strongest demo artifact for the writeup: "free models shipped a tested fix to a real TypeScript codebase for $0."

Builds on [[2026-06-12 stage-3-engine-built]], [[2026-06-12 coder-context-slimming]].
