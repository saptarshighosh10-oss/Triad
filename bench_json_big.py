"""BIG test: build a JSON parser from scratch, graded against Python's json.loads.

Head-to-head on the SAME hard task + SAME oracle:
  A) baseline   — one strong free model (GPT-OSS-120B) builds the whole parser in one shot
  B) swarm      — director decomposes → free workers solve pieces → director integrates

The question: does recursive decomposition help on a TIGHTLY-COUPLED build, or does
interface mismatch between independently-generated pieces make one strong worker win?
"""
import asyncio
import tempfile
from pathlib import Path

from triad import dotenv
dotenv.load(Path(__file__).resolve().parent / ".env")

from triad.agents import build_agents, OpenRouterAgent
from triad.orchestrator import Orchestrator
from triad.oracle import CommandOracle, extract_code
from triad.sandbox import Sandbox
from rich.console import Console

TASK = (
    "Implement parse(s) in solution.py: a JSON parser that takes a JSON string and returns the "
    "equivalent Python object (dict, list, str, int, float, bool, None) — exactly like json.loads. "
    "Support: objects {}, arrays [], strings with escapes (\\\" \\\\ \\/ \\n \\t \\r \\b \\f and "
    "\\uXXXX unicode), numbers (integers, negatives, decimals, exponents like 1e10 and 2.5E-3), "
    "true, false, null, and arbitrary nesting and whitespace. Raise an exception on invalid JSON. "
    "Do NOT import the json module — write the parser yourself."
)

TEST = r'''
import json, sys
try:
    import solution
except Exception as e:
    print("IMPORT_FAIL:", e); sys.exit(1)
parse = getattr(solution, "parse", None)
if not callable(parse):
    print("NO parse() FUNCTION"); sys.exit(1)

# Build valid cases from real Python objects -> json.dumps gives correctly-escaped JSON for free
# (avoids hand-escaping bugs). Each object exercises a feature; parse(dumps(obj)) must == obj.
objects = [
    {}, [], {"a": 1}, {"a": 1, "b": 2}, [1, 2, 3],
    {"nested": {"x": [1, 2, {"y": True}]}}, "hello", 'has "quotes" inside',
    "tab\there", "newline\nhere", "back\\slash", "café-unicode-é",
    123, -456, 3.14, -0.001, True, False, None,
    {"arr": [], "obj": {}}, [None, True, False],
    {"mixed": [1, "two", 3.0, None, {"k": "v"}]}, {"empty_str": ""},
    [[1], [2], [3]], {"deep": {"a": {"b": {"c": {"d": 1}}}}},
    {"num": 0}, ["a", "b", "c"], {"t": True, "f": False, "n": None},
    {"emoji": "smile", "ctrl": "a\tb\nc"}, list(range(20)),
]
# Hand-written raw-JSON cases (number formats + whitespace json.dumps won't emit)
raw_valid = ["1e10", "2.5e-3", "1.5E+3", "   {  \"spaced\"  :  42  }   ", "-0", "0.0"]
invalid = ['{bad}', '[1,]', '{"a":}', "tru", '{"x" 1}', "[1 2]", '{"k": "v",}']

fails = 0; first = None; total = 0
for obj in objects:
    total += 1
    s = json.dumps(obj)
    try:
        ok = (parse(s) == obj)
    except Exception as e:
        ok = False; obj = f"RAISED {type(e).__name__}"
    if not ok:
        fails += 1
        if first is None: first = ("valid", s[:50], "mismatch")
for s in raw_valid:
    total += 1
    try:
        ok = (parse(s) == json.loads(s))
    except Exception:
        ok = False
    if not ok:
        fails += 1
        if first is None: first = ("raw-valid", s[:50], "mismatch")
for s in invalid:
    total += 1
    try:
        parse(s); fails += 1
        if first is None: first = ("should-raise", s[:50], "no exception")
    except Exception:
        pass  # correctly rejected

print(f"checked {total} cases ({len(objects)+len(raw_valid)} valid + {len(invalid)} invalid)")
if fails:
    print(f"FAIL: {fails} wrong; first: {first}")
    sys.exit(1)
print("ALL PASS"); sys.exit(0)
'''

STUB = "def parse(s):\n    raise NotImplementedError\n"


def make_oracle():
    return CommandOracle("python3 test_json.py", fixtures={"test_json.py": TEST}, timeout=40)


async def baseline(console):
    """One strong free model builds the whole parser; oracle-check it."""
    console.rule("[bold]A) BASELINE — single strong worker (GPT-OSS-120B)[/bold]")
    a = OpenRouterAgent()
    a.model = "openai/gpt-oss-120b:free"
    raw = await a.complete_raw(
        [{"role": "user", "content": TASK + "\n\nReturn ONLY the Python code in one block."}],
        max_tokens=3000)
    code = extract_code(raw)
    v = make_oracle().check(f"```python\n{code}\n```", Sandbox())
    console.print(f"baseline: [bold]{'PASS' if v.passed else 'FAIL'}[/bold] — {v.detail}")
    return v.passed


async def swarm_run(console):
    """Director decomposes → free workers → integrate → verify integrated result."""
    console.rule("[bold]B) SWARM — decompose → workers → integrate[/bold]")
    agents = build_agents("free")
    orch = Orchestrator(agents, {}, console, mode="swarm")
    orch.max_depth = 1
    orch.history_limit = 0
    orch.oracle = make_oracle()
    await orch.run_swarm(TASK)
    last = orch.transcript[-1] if orch.transcript else ""
    return "PASS" in last and "FAIL" not in last.split("PASS")[0][-10:]


async def main():
    console = Console()
    b = await baseline(console)
    s = await swarm_run(console)
    console.rule("[bold]VERDICT[/bold]")
    console.print(f"baseline (1 strong worker): [bold]{'PASS' if b else 'FAIL'}[/bold]")
    console.print(f"swarm (decompose+integrate): [bold]{'PASS' if s else 'FAIL'}[/bold]")


if __name__ == "__main__":
    asyncio.run(main())
