# 2026-06-12 benchmark-result

First real run of the Stage 3c benchmark (`triad bench --free`). The number that turns the project into a measurement. Back to [[index]].

## Setup
- 16 verifiable coding tasks, each with a ground-truth oracle (8 easy + 8 spec-heavy/edge-casey).
- 2 free heads, distinct lineages: **Groq** (llama-3.3-70b) + **OpenRouter** (gpt-oss-120b).
- Each task: one EditJob run; per-model single-shot pass + three-head select (best-of-N) + three-head with bounded critique-revise (rounds=2). Gated in the macOS-seatbelt sandbox. $0.

## Result
| | Groq | OpenRouter | 3-head select | 3-head + revise |
|---|---|---|---|---|
| pass rate | 93.75% | 93.75% | **100%** | **100%** |

- Groq missed **`evaluate`** (integer expression parser with precedence).
- OpenRouter missed **`roman_to_int`**.
- **They failed on DIFFERENT tasks** — so the oracle-checked union cleared all 16.

**Headline:** best single free model **94%** → three-head select **100%**, over 16 verifiable tasks, $0.

## Why this is the thesis, demonstrated
This is exactly [[cross-provider decorrelation]] + [[generate-verify-select]] in one picture: neither free model is individually reliable, and — the load-bearing part — their errors are **not correlated** (different blind spots). An executable oracle ([[oracle independence]]) lets you safely *select* whichever head is right, so the union beats the best individual. A single model with the same oracle can only resubmit its own correlated guess; three diverse heads cover each other. The win is real *because* the failures don't overlap — which is the whole argument for diverse lineages over three finetunes of one base.

## CORRECTION — 3-seed run with the NIM 3rd head (supersedes the single-seed headline above)
Added NIM (minimax-m3) as a 3rd lineage and ran **3 seeds × 16 tasks** (rounds=1). Averaged:
- Groq **100% ±0**, OpenRouter **100% ±0**, NIM **90% ±11 (75–100%)**.
- best single **100%** → three-head select **100%** → **no measurable lift**.

Honest read: the single-seed "94% → 100%" was a **lucky sample** (that seed, two heads happened to fail different tasks). Averaged, Groq and OpenRouter already solo this suite, so verify-select adds nothing here — and NIM/minimax-m3 is the weak, high-variance head, not OpenRouter "winning". **The suite is too easy to show the aggregation win.** This isn't a failure of the thesis; it's the boundary of where it applies: the three-head/oracle win materializes only when single models *reliably fail* (hard multi-step/parsing/DP/edge-case tasks, 20–40% single-model failure). Building a hard tier that pulls single-model rates down is now the prerequisite for a defensible pass-rate number. Multi-seed (`--seeds`) is what surfaced this — exactly its job.

## Honest caveats (state these — they make the result credible, not weaker)
- **Single run, stochastic.** Free models vary run-to-run; one run isn't a confidence interval. Rigorous version: N seeds per task, report mean ± variance.
- **n=16, 2 heads.** Small. The 3rd lineage (NIM/Qwen, needs a key) isn't in this run — adding it should widen the gap and is the cheapest next step.
- **Modest magnitude (94→100).** The *mechanism* is shown cleanly (disjoint failures), but to show a bigger gap you need harder tasks and/or weaker models. Difficulty calibration is itself part of the method: the first 8 tasks saturate at 100% and carry no signal — all the signal lives in the hard tier.
- **Verifiable tasks only.** This measures exactly where free aggregation is supposed to win (code with a ground-truth check). It says nothing about subjective tasks — paid `council` stays the right tool there.

## To strengthen for a writeup (3c+)
1. ~~Multi-seed runs → pass-rate ± variance~~ **BUILT** — `triad bench --seeds N` (`run_multi`/`aggregate_runs`/`format_multi`): repeats the suite N times, reports **mean ± stdev + min–max** per metric. Live-confirmed on the easy slice (saturates at ±0); run the full hard suite with `--seeds 3+` to get real spread. 168/168 tests (+5 aggregation).
2. Add the NIM/Qwen head (3 lineages) → measure whether disjoint coverage widens the gap.
3. Harder task tier (parsers, DP, tricky specs) to pull single-model rates down where the union win is larger.
4. An ablation: select-only vs +revise, to separate "diversity covers it" from "retrying fixes it."

Raw JSON saved at run time via `--json`. Reproduce: `python -m triad bench --free --rounds 2 --json out.json`.

Built in [[2026-06-12 stage-3-engine-built]].
