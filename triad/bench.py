"""Benchmark: does three-head verify-select beat a single free model? (Stage 3c)

This is the result that turns the project into a *measurement*. Each task is a tiny, verifiable
coding problem carrying its own oracle. For every task, from ONE EditJob run (so the numbers are
internally consistent and we don't pay for extra generations), we record on the SAME oracle:

  * per-model single-shot — each free head generates once; did its edit pass the oracle?
  * three-head select     — did ANY head pass on the first shot? (best-of-N; the decorrelation win)
  * three-head + revise   — did the full generate-verify-select loop end VERIFIED within the bound?

The headline is **best-single-model%  vs  three-head%**. The thesis: no single free model is
reliable, but three *distinct lineages* ([[cross-provider decorrelation]]) pass different subsets,
so their oracle-checked union beats the best individual — aggregation paying off, made measurable.
A single model with the same oracle can only resubmit its own correlated guess; the heads cover
each other. Honest scope: this measures **verifiable** tasks (code with a ground-truth check) —
exactly where free aggregation is supposed to win, and where the oracle makes "select" meaningful.
"""
from __future__ import annotations

import asyncio
import io
import statistics
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from rich.console import Console
from rich.table import Table

from .agents import Agent
from .coder import EditJob, read_repo
from .oracle import CommandOracle
from .sandbox import Sandbox


@dataclass
class Task:
    name: str
    prompt: str
    oracle: str                                   # command run in the sandbox; exit 0 == pass
    files: Dict[str, str] = field(default_factory=dict)   # starting repo state


def _check(expr: str) -> str:
    return f'python3 -c "import solution as s; assert {expr}; print(\'ok\')"'


# A small suite of verifiable problems. Mix of fix-the-bug and implement-from-scratch, each with a
# ground-truth oracle (python3 asserts — no pytest dependency needed in the sandbox).
DEFAULT_TASKS: List[Task] = [
    Task("fix_add", "Fix the add function in solution.py so it returns the sum of a and b.",
         _check("s.add(2, 3) == 5 and s.add(-1, 1) == 0"),
         {"solution.py": "def add(a, b):\n    return a - b\n"}),
    Task("factorial", "In solution.py, implement factorial(n) returning n! (factorial(0) == 1).",
         _check("s.factorial(5) == 120 and s.factorial(0) == 1")),
    Task("fizzbuzz", "In solution.py, implement fizzbuzz(n): 'Fizz' if divisible by 3, 'Buzz' if by 5, "
                     "'FizzBuzz' if by both, else str(n).",
         _check("s.fizzbuzz(3)=='Fizz' and s.fizzbuzz(5)=='Buzz' and s.fizzbuzz(15)=='FizzBuzz' and s.fizzbuzz(7)=='7'")),
    Task("reverse_string", "In solution.py, implement reverse_string(s) returning the string reversed.",
         _check("s.reverse_string('abc')=='cba' and s.reverse_string('')==''")),
    Task("is_palindrome", "In solution.py, implement is_palindrome(s) -> bool (case-sensitive, exact).",
         _check("s.is_palindrome('racecar') is True and s.is_palindrome('abc') is False")),
    Task("max_in_list", "In solution.py, implement max_in_list(xs) returning the largest item WITHOUT "
                        "using the built-in max().",
         _check("s.max_in_list([3,1,2])==3 and s.max_in_list([-5,-2,-9])==-2")),
    Task("count_vowels", "In solution.py, implement count_vowels(s) counting a, e, i, o, u "
                         "(lowercase only).",
         _check("s.count_vowels('hello')==2 and s.count_vowels('xyz')==0")),
    Task("gcd", "In solution.py, implement gcd(a, b) returning the greatest common divisor.",
         _check("s.gcd(12, 8)==4 and s.gcd(17, 5)==1")),

    # ---- harder tier: spec-heavy / edge-casey, where free 70B-class models get flaky. This is
    # where the gap between a single model and the oracle-checked three-head union shows up. ----
    Task("roman_to_int", "In solution.py, implement roman_to_int(s) converting an UPPERCASE roman "
                         "numeral to an int (handle subtractive pairs like IV, IX, XC, CM).",
         _check("s.roman_to_int('MCMXCIV')==1994 and s.roman_to_int('IV')==4 and s.roman_to_int('LVIII')==58")),
    Task("evaluate", "In solution.py, implement evaluate(expr): a string of integers with + - * "
                     "(no parentheses), respecting operator precedence, returning an int. Do NOT use eval().",
         _check("s.evaluate('3+5*2')==13 and s.evaluate('10-2-3')==5 and s.evaluate('2*3+4*5')==26")),
    Task("my_atoi", "In solution.py, implement my_atoi(s): skip leading spaces, optional +/- sign, read "
                    "digits until a non-digit, return the int (0 if no digits).",
         _check("s.my_atoi('   -42')==-42 and s.my_atoi('4193 with words')==4193 and "
                "s.my_atoi('words and 987')==0 and s.my_atoi('+1')==1")),
    Task("valid_parentheses", "In solution.py, implement valid_parentheses(s) -> bool: True iff "
                              "brackets ()[]{} are correctly matched and nested.",
         _check("s.valid_parentheses('()[]{}') is True and s.valid_parentheses('(]') is False and "
                "s.valid_parentheses('([)]') is False and s.valid_parentheses('{[]}') is True")),
    Task("merge_intervals", "In solution.py, implement merge_intervals(intervals): merge all "
                            "overlapping [start,end] intervals, returned sorted by start.",
         _check("s.merge_intervals([[1,3],[2,6],[8,10],[15,18]])==[[1,6],[8,10],[15,18]] and "
                "s.merge_intervals([[1,4],[4,5]])==[[1,5]]")),
    Task("spiral_order", "In solution.py, implement spiral_order(matrix) returning all elements in "
                         "clockwise spiral order as a list.",
         _check("s.spiral_order([[1,2,3],[4,5,6],[7,8,9]])==[1,2,3,6,9,8,7,4,5] and "
                "s.spiral_order([[1,2],[3,4]])==[1,2,4,3]")),
    Task("caesar_cipher", "In solution.py, implement caesar_cipher(text, shift): shift letters by "
                          "`shift`, wrapping within case, leaving non-letters unchanged.",
         _check("s.caesar_cipher('abcXYZ',3)=='defABC' and s.caesar_cipher('Hello, World!',1)=='Ifmmp, Xpsme!'")),
    Task("fix_binary_search", "Fix the off-by-one bug in binary_search so it returns the index of "
                              "target in the sorted list, or -1 if absent.",
         _check("s.binary_search([1,3,5,7,9],7)==3 and s.binary_search([1,3,5,7,9],2)==-1 and "
                "s.binary_search([],1)==-1"),
         {"solution.py": "def binary_search(xs, target):\n"
                         "    lo, hi = 0, len(xs)\n"
                         "    while lo < hi:\n"
                         "        mid = (lo + hi) // 2\n"
                         "        if xs[mid] == target:\n"
                         "            return mid\n"
                         "        elif xs[mid] < target:\n"
                         "            hi = mid          # BUG: should move lo\n"
                         "        else:\n"
                         "            lo = mid + 1      # BUG: should move hi\n"
                         "    return -1\n"}),
]


@dataclass
class TaskOutcome:
    name: str
    per_model: Dict[str, bool]      # model label -> passed single-shot (round 1)
    select: bool                    # any model passed round 1 (best-of-N)
    revise: bool                    # full EditJob ended VERIFIED within the bound


@dataclass
class BenchReport:
    models: List[str]
    outcomes: List[TaskOutcome]
    rounds: int

    @property
    def n(self) -> int:
        return len(self.outcomes)

    def model_rate(self, label: str) -> float:
        if not self.outcomes:
            return 0.0
        return 100.0 * sum(o.per_model.get(label, False) for o in self.outcomes) / self.n

    def best_single(self) -> float:
        return max((self.model_rate(m) for m in self.models), default=0.0)

    def select_rate(self) -> float:
        return 100.0 * sum(o.select for o in self.outcomes) / self.n if self.outcomes else 0.0

    def revise_rate(self) -> float:
        return 100.0 * sum(o.revise for o in self.outcomes) / self.n if self.outcomes else 0.0


def _quiet() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=100)


async def run_benchmark(agents: List[Agent], tasks: Optional[List[Task]] = None,
                        console: Optional[Console] = None, sandbox: Optional[Sandbox] = None,
                        rounds: int = 3) -> BenchReport:
    """Run every task through one EditJob each; read per-model round-1 verdicts + the final result."""
    tasks = tasks or DEFAULT_TASKS
    console = console or Console()
    sandbox = sandbox or Sandbox()
    labels = [a.label for a in agents]
    console.print(f"[dim]benchmark: {len(tasks)} tasks · {len(agents)} heads ({', '.join(labels)}) · "
                  f"sandbox={sandbox.tier} · rounds={rounds}[/dim]")

    outcomes: List[TaskOutcome] = []
    for i, task in enumerate(tasks, 1):
        with tempfile.TemporaryDirectory(prefix="triad-bench-") as d:
            root = Path(d)
            for p, body in task.files.items():
                fp = root / p
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(body, encoding="utf-8")
            job = EditJob(agents, oracle=CommandOracle(task.oracle), console=_quiet(),
                          sandbox=sandbox, rounds=rounds)
            try:
                result = await job.run(task.prompt, str(root))
            except Exception as e:
                console.print(f"  [{i}/{len(tasks)}] {task.name}: [red]error {type(e).__name__}: {e}[/red]")
                outcomes.append(TaskOutcome(task.name, {m: False for m in labels}, False, False))
                continue

        round1 = job.round_log[0] if job.round_log else []
        per_model = {c.label: bool(c.verdict and c.verdict.passed) for c in round1}
        for m in labels:
            per_model.setdefault(m, False)
        select = any(per_model.values())
        outcome = TaskOutcome(task.name, per_model, select, result.verified)
        outcomes.append(outcome)
        cells = " ".join(("[green]✓[/green]" if per_model[m] else "[red]✗[/red]") for m in labels)
        tag = "[green]✓ revise[/green]" if outcome.revise else "[red]✗[/red]"
        console.print(f"  [{i}/{len(tasks)}] {task.name:<16} heads {cells}  "
                      f"select {'[green]✓[/green]' if select else '[red]✗[/red]'}  {tag}")
    return BenchReport(labels, outcomes, rounds)


def format_report(report: BenchReport) -> Table:
    t = Table(title=f"three-head verify-select benchmark · {report.n} tasks · {report.rounds} round(s)")
    t.add_column("task", style="bold")
    for m in report.models:
        t.add_column(m, justify="center")
    t.add_column("3-head\nselect", justify="center")
    t.add_column("3-head\n+revise", justify="center")
    yes, no = "[green]✓[/green]", "[red]·[/red]"
    for o in report.outcomes:
        row = [o.name] + [yes if o.per_model.get(m) else no for m in report.models]
        row += [yes if o.select else no, yes if o.revise else no]
        t.add_row(*row)
    rates = ["[bold]pass rate[/bold]"] + [f"[bold]{report.model_rate(m):.0f}%[/bold]" for m in report.models]
    rates += [f"[bold]{report.select_rate():.0f}%[/bold]", f"[bold]{report.revise_rate():.0f}%[/bold]"]
    t.add_section()
    t.add_row(*rates)
    return t


# ----------------------------------------------------------- token-savings benchmark
# Representative questions a session might ask its own memory — used to measure recall savings.
DEFAULT_QUERIES = [
    "how do the free models avoid correlated errors",
    "how is untrusted model code run safely",
    "what is the verify-select file editing result",
    "why not invent a brand new compressed language",
    "what is the target audience and budget goal",
]


def vault_recall_savings(memory, queries=None):
    """Per-query: tokens to dump the whole vault ('re-read everything') vs recall the relevant slice."""
    queries = queries or DEFAULT_QUERIES
    rows = []
    for q in queries:
        sv = memory.savings(q)
        rows.append((q, sv["full_tokens"], sv["recall_tokens"], sv["saved_pct"]))
    return rows


def history_growth_savings(turns=20, turn_tokens=300, limit=6, recall_tokens=400):
    """Analytic model of the re-send cost over a K-turn chat (assumptions stated, no API calls).

    Unbounded: turn i re-sends all i prior+current exchanges -> sum_i i*T  = O(K^2).
    Bounded+recall: turn i sends min(i,limit) exchanges + a fixed recall slice -> ~O(K).
    """
    baseline = sum(i * turn_tokens for i in range(1, turns + 1))
    bounded = sum(min(i, limit) * turn_tokens + recall_tokens for i in range(1, turns + 1))
    pct = int(100 * (baseline - bounded) / baseline) if baseline else 0
    return {"turns": turns, "turn_tokens": turn_tokens, "limit": limit, "recall_tokens": recall_tokens,
            "baseline": baseline, "bounded": bounded, "saved_pct": pct}


def format_token_report(rows, growth) -> Table:
    t = Table(title="token-savings · recall-over-re-read")
    t.add_column("memory query", style="bold")
    t.add_column("whole vault", justify="right")
    t.add_column("recall", justify="right")
    t.add_column("saved", justify="right")
    full_sum = rec_sum = 0
    for q, full, rec, pct in rows:
        full_sum += full
        rec_sum += rec
        t.add_row((q[:42] + "…") if len(q) > 43 else q, f"{full}", f"{rec}", f"[green]{pct}%[/green]")
    t.add_section()
    avg = int(100 * (full_sum - rec_sum) / full_sum) if full_sum else 0
    t.add_row("[bold]average[/bold]", f"[bold]{full_sum // max(1,len(rows))}[/bold]",
              f"[bold]{rec_sum // max(1,len(rows))}[/bold]", f"[bold]{avg}%[/bold]")
    return t


def token_headline(rows, growth) -> str:
    full_sum = sum(r[1] for r in rows)
    rec_sum = sum(r[2] for r in rows)
    avg = int(100 * (full_sum - rec_sum) / full_sum) if full_sum else 0
    return (f"recall vs re-read the whole vault: ~{avg}% fewer tokens per turn   ·   "
            f"a {growth['turns']}-turn chat: {growth['baseline']:,} tokens re-sent "
            f"→ {growth['bounded']:,} with bounded history + recall ({growth['saved_pct']}% less)")


def headline(report: BenchReport) -> str:
    return (f"best single free model: {report.best_single():.0f}%   →   "
            f"three-head select: {report.select_rate():.0f}%   →   "
            f"three-head + revise: {report.revise_rate():.0f}%   "
            f"(over {report.n} verifiable tasks, $0)")


# ------------------------------------------------------ multi-seed (variance)
def _stat(vals: List[float]) -> Dict[str, float]:
    return {"mean": statistics.fmean(vals), "sd": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "min": min(vals), "max": max(vals)}


def aggregate_runs(reports: List[BenchReport]) -> Dict:
    """Mean ± stdev (and min/max) of each metric across repeated runs — variance, not a single bit.

    Free models are stochastic, so one run is a sample, not a result. Repeating the suite and
    reporting spread is what makes the pass-rate claim defensible rather than anecdotal."""
    models = reports[0].models if reports else []
    return {
        "seeds": len(reports), "tasks": reports[0].n if reports else 0,
        "best_single": _stat([r.best_single() for r in reports]),
        "select": _stat([r.select_rate() for r in reports]),
        "revise": _stat([r.revise_rate() for r in reports]),
        "per_model": {m: _stat([r.model_rate(m) for r in reports]) for m in models},
    }


async def run_multi(agents: List[Agent], tasks=None, console=None, sandbox=None,
                    rounds: int = 3, seeds: int = 3) -> List[BenchReport]:
    console = console or Console()
    sandbox = sandbox or Sandbox()
    reports: List[BenchReport] = []
    for s in range(1, seeds + 1):
        console.rule(f"[bold]seed {s}/{seeds}[/bold]")
        reports.append(await run_benchmark(agents, tasks=tasks, console=console,
                                           sandbox=sandbox, rounds=rounds))
    return reports


def format_multi(agg: Dict) -> Table:
    t = Table(title=f"multi-seed benchmark · {agg['seeds']} seeds × {agg['tasks']} tasks")
    t.add_column("metric", style="bold")
    t.add_column("mean", justify="right")
    t.add_column("± sd", justify="right")
    t.add_column("range", justify="right")
    def row(label, s):
        t.add_row(label, f"{s['mean']:.0f}%", f"±{s['sd']:.0f}", f"{s['min']:.0f}–{s['max']:.0f}%")
    for m, s in agg["per_model"].items():
        row(m, s)
    t.add_section()
    row("3-head select", agg["select"])
    row("3-head + revise", agg["revise"])
    return t


def multi_headline(agg: Dict) -> str:
    b, s, v = agg["best_single"], agg["select"], agg["revise"]
    return (f"over {agg['seeds']} seeds — best single free model: {b['mean']:.0f}% ±{b['sd']:.0f}   →   "
            f"three-head select: {s['mean']:.0f}% ±{s['sd']:.0f}   →   "
            f"+revise: {v['mean']:.0f}% ±{v['sd']:.0f}")
