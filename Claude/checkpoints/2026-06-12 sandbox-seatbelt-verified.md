# 2026-06-12 sandbox-seatbelt-verified

Hardened the [[execution sandbox]]: the macOS no-net floor (sandbox-exec, deprecated) is now VERIFIED at startup with a real probe, not assumed. If it stops enforcing it degrades to subprocess + warns (network_blocked=False) — fail-loud, never silent fail-open. Tested. See [[stage 2 verify-select]].

Back to [[index]].
