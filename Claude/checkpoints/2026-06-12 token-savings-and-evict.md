# 2026-06-12 token-savings-and-evict

Two builds closing out the token/memory work for the capped-tier audience. Back to [[index]].

## Summarize-on-evict (bounded history is now lossless against the vault)
When `_trim_history` drops old turns, the orchestrator hands them to `on_evict(agent, dropped)`. The REPL wires that to `vault.archive_evicted`, which summarizes each evicted Q→A exchange into `memory/<name>.md`. VaultMemory indexes `memory/*.md`, so a trimmed turn can be **pulled back later by recall** instead of being gone — bounded history loses nothing the vault can't return. Best-effort (never breaks a turn). Tested: on_evict fires on overflow, payload carries the dropped exchange, the archived note is written, and recall finds the evicted turn.

## Token-savings benchmark (the second defensible number)
`triad bench --tokens [--vault DIR]` — measurement only, **no API calls**. Two parts:
- **Recall vs re-read (real, against the vault):** per representative query, tokens to dump the whole vault vs recall the relevant slice. **LIVE on this vault (27 notes, ~8269 tok): ~96% fewer tokens per turn** (recall ~314 vs 8269 avg).
- **History-growth (analytic, assumptions stated):** unbounded re-send is O(K²) (turn i re-sends all i exchanges → Σ i·T); bounded+recall is ~O(K). A 20-turn chat: 63,000 tokens re-sent → 39,500 with cap-6 + recall (**37% less**), and — the defensible claim — **the saving grows with length** (≈72% by 50 turns). Tested that savings increase with turns.

## Why this matters
This is the budget headline alongside the pass-rate one: the project doesn't just get free/cheap models to *work* (94→100% verify-select), it makes a limited context window *last* (~96% less per recall, quadratic→linear over a long chat). Two numbers, two audiences ($0 and capped-tier paid), one toolkit.

## Status / still open
- 158/158 offline tests. Live-verified both.
- Reproduce: `python -m triad bench --tokens --vault Claude`.
- Open: embedding-tier retrieval (opt-in); apply recall to the coder's whole-repo context (still the biggest single-shot token sink); multi-seed pass-rate bench + NIM 3rd head.

Builds on [[2026-06-12 vault-memory-recall]].
