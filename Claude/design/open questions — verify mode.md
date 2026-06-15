# open questions — verify mode

Conservative decisions made while building [[stage 2 verify-select]] (oracle.py + run_verify), logged so they can be revisited rather than silently assumed. Back to [[index]].

1. **Candidate extraction.** Each candidate's code = the *largest fenced block* (`extract_code`), written as `solution.py`. Multi-file outputs and non-Python languages aren't handled yet. Conservative: single-file Python.

2. **Oracle command tool access.** Oracle commands run in the [[execution sandbox]]'s *scrubbed* env (`PATH=/usr/bin:/bin`), so `python3` works but `pytest` must be on the system PATH or called by full path. Tension: richer tool access vs. keeping untrusted code's env clean. Open: a venv-aware oracle mode.

3. **Fixtures / test files.** `--oracle "pytest -q"` needs the user's test files in the sandbox. Right now the oracle command must be self-contained or reference `solution.py` (e.g. `python3 -c "import solution; assert ..."`); auto-copying CWD test files is NOT wired (deliberately — no surprise CWD scans). Open: an explicit `--oracle-fixtures <dir>`.

4. **Tie-break among passers.** If several candidates pass, we pick the *first in agent order* (stable, transparent). Open: shortest / most-common / chair-pick. Never affects correctness — all picked are verified passers.

5. **No-oracle "selection".** With no oracle we generate, show all candidates, and stamp **UNVERIFIED — selection only**; we do NOT auto-pick a winner (avoids implying verification). [[oracle independence]] holds: nothing self-grades. Open: an optional chair-synthesis selection, clearly labelled unverified.

6. **TestAuthorOracle.** A separate model that writes tests *before* seeing candidates would be a second independent oracle. The `Oracle` interface is ready; not built this pass.
