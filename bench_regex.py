"""Hard benchmark: implement a regex engine from scratch, graded against Python's `re`.

The candidate must implement match(pattern, text) -> bool (FULL match) supporting:
  literals, . (any char), * + ? quantifiers, (...) grouping, and | alternation,
with correct precedence (concat binds tighter than |, quantifier binds to prior atom).

The oracle exhaustively compares the candidate against re.fullmatch over ~5,400 cases.
The test is injected as a sandbox FIXTURE, so a candidate can't overwrite it to cheat.
"""
import asyncio
import tempfile
from pathlib import Path

from triad import dotenv
dotenv.load(Path(__file__).resolve().parent / ".env")

from triad.agents import build_agents
from triad.coder import EditJob
from triad.oracle import CommandOracle
from rich.console import Console

TASK = (
    "Implement match(pattern, text) in solution.py: a regular-expression engine that returns "
    "True iff `text` is FULLY matched by `pattern` (like re.fullmatch, not a partial/search). "
    "Support these features: literal characters; '.' matches any single character; '*' (zero or "
    "more), '+' (one or more), and '?' (zero or one) quantifiers applied to the immediately "
    "preceding element; '(' ')' for grouping; and '|' for alternation. Precedence: concatenation "
    "binds tighter than '|', and a quantifier applies to the single preceding atom or group. "
    "Quantifiers and alternation must work on groups, e.g. (a|b)*c and (a|bc)* must work. "
    "Do NOT import the `re` module or any regex library — write the engine yourself."
)

TEST = r'''
import re, sys, itertools
try:
    import solution
except Exception as e:
    print("IMPORT_FAIL:", e); sys.exit(1)
m = getattr(solution, "match", None)
if not callable(m):
    print("NO match() FUNCTION"); sys.exit(1)

patterns = [
    "a*b", "a+b", "ab?c", "a.c", ".*", "(a|b)*c", "(ab)+",
    "((a|b)c)*", "a|b|c", ".*a.*", "(a|b)(c|d)", "a(b|c)*d",
    "(abc)*", "a.*b", "(a|bc)*", "x*",
]
alpha = "abcd"
strings = [""]
for L in range(1, 5):
    for t in itertools.product(alpha, repeat=L):
        strings.append("".join(t))

fails = 0; first = None; total = 0
for p in patterns:
    for s in strings:
        total += 1
        want = re.fullmatch(p, s) is not None
        try:
            got = bool(m(p, s))
        except Exception:
            got = None
        if got != want:
            fails += 1
            if first is None:
                first = (p, s, want, got)
print(f"checked {total} cases across {len(patterns)} patterns")
if fails:
    print(f"FAIL: {fails} mismatches; first: pattern={first[0]!r} str={first[1]!r} "
          f"want={first[2]} got={first[3]}")
    sys.exit(1)
print("ALL PASS"); sys.exit(0)
'''

STUB = "def match(pattern, text):\n    raise NotImplementedError\n"


async def main():
    console = Console()
    agents = build_agents("free")
    console.print(f"heads: {[(a.label, a.model) for a in agents]}")
    oracle = CommandOracle("python3 test_regex.py",
                           fixtures={"test_regex.py": TEST}, timeout=40)
    with tempfile.TemporaryDirectory(prefix="regex-bench-") as d:
        (Path(d) / "solution.py").write_text(STUB)
        job = EditJob(agents, oracle=oracle, console=console, rounds=3)
        result = await job.run(TASK, d)

    console.rule("[bold]RESULTS[/bold]")
    for rnd_i, cands in enumerate(job.round_log, 1):
        console.print(f"[bold]Round {rnd_i}[/bold]")
        for c in cands:
            v = c.verdict
            status = "PASS" if (v and v.passed) else "fail"
            detail = (v.detail if v else "")[:120]
            console.print(f"  {c.label:14} {status:5} {detail}")
    console.rule()
    console.print(f"FINAL: [bold]{result.status}[/bold] — {result.detail}"
                  + (f"  (winner: {result.winner})" if result.winner else ""))


if __name__ == "__main__":
    asyncio.run(main())
