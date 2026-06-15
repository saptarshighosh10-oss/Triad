"""Interactive REPL.

Plain text -> broadcast as a task in the current mode.
/commands  -> control modes, skills, agents, history.

A single asyncio event loop runs the whole session; blocking input() is offloaded
to a thread so streaming stays responsive and provider clients stay bound to one loop.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Dict, List

from rich.console import Console

from . import cooldown, dotenv, keychain, vault
from .agents import Agent, build_agents
from . import config
from .orchestrator import Orchestrator
from .setup_keys import CORE, EXTRA, run_setup
from .skills import Skill, load_skills

DEFAULT_ENV = Path(__file__).resolve().parent.parent / ".env"

HELP = """[bold]Commands[/bold]
  /mode parallel|relay|council|verify   switch how agents collaborate
  /code <task>                   three-head verify-select EDIT of files in cwd (uses /oracle)
  /oracle <cmd> | off            pass condition for verify + /code (e.g. /oracle "pytest -q")
  /protocol on|off               compact handoffs in relay/council (saves tokens)
  /skill <name> [agent|all]      apply a skill (default target = its frontmatter)
  /skills                        list available skill files
  /clearskills                   strip all applied skills
  /agents                        list active agents + models
  /keys                          add or fix an API key (re-runs setup)
  /reset                         clear conversation history
  /recall <query>                retrieve only the relevant vault notes (memory; saves re-reading)
  /memory on|off                 auto-recall vault notes each turn + cap history (recall over re-read)
  /remember                      write the session to the Obsidian vault now (needs --vault)
  /save [file.md]                save transcript
  /help                          this help
  /quit                          exit

[bold]Modes[/bold]
  parallel  same task to all three at once, independent answers side by side
  relay     agents work in sequence, each building on the previous output
  council   all answer, then a chair agent synthesizes the best single answer
  verify    generate candidates, run each against an oracle in the sandbox, keep a passer
            (critique-revise if none); no oracle -> "unverified, selection only" (set with /oracle)
"""


def _apply_skill(parts: List[str], agents: List[Agent], skills: Dict[str, Skill], console: Console):
    if len(parts) < 2:
        console.print("usage: /skill <name> [agent|all]")
        return
    sk = skills.get(parts[1])
    if not sk:
        console.print(f"[red]no skill named '{parts[1]}'[/red] — try /skills")
        return
    targets = [parts[2]] if len(parts) > 2 else sk.agents
    applied = []
    for a in agents:
        if "all" in targets or a.name in targets:
            a.add_skill(sk.body)
            applied.append(a.label)
    console.print(f"applied [bold]{sk.name}[/bold] -> {', '.join(applied) or '(no agent matched)'}")


def _activate_new(agents: List[Agent], console: Console, roster: str = "paid") -> None:
    """Append providers whose keys just appeared. `agents` is the live list shared with
    the orchestrator, so append exactly once — appending to both aliases double-adds."""
    have = {a.name for a in agents}
    for a in build_agents(roster):
        if a.name not in have:
            agents.append(a)
            console.print(f"added [bold {a.color}]{a.label}[/]")


def _save(orch: Orchestrator, filename: str, console: Console):
    if not orch.transcript:
        console.print("[dim]nothing to save yet[/dim]")
        return
    body = "# triad transcript\n\n" + "\n".join(orch.transcript)
    with open(filename, "w", encoding="utf-8") as fh:
        fh.write(body)
    console.print(f"saved -> [bold]{filename}[/bold]")


def _command(line: str, orch: Orchestrator, agents, skills, console) -> bool:
    """Return True to quit."""
    parts = line.split()
    c = parts[0].lower()
    if c in ("/quit", "/exit", "/q"):
        return True
    if c == "/help":
        console.print(HELP)
    elif c == "/mode":
        if len(parts) > 1 and parts[1] in ("parallel", "relay", "council", "verify"):
            orch.mode = parts[1]
            console.print(f"mode -> [bold]{orch.mode}[/bold]")
        else:
            console.print("usage: /mode parallel|relay|council|verify")
    elif c == "/oracle":
        if len(parts) > 1 and parts[1].lower() in ("off", "none", "clear"):
            orch.oracle = None
            console.print("oracle cleared — verify is now [yellow]unverified (selection only)[/yellow]")
        elif len(parts) > 1:
            from .oracle import CommandOracle
            cmd = line.split(None, 1)[1]
            orch.oracle = CommandOracle(cmd)
            console.print(f"oracle set: [bold]{cmd}[/bold]  (use it in [bold]/mode verify[/bold])")
        else:
            cur = orch.oracle.describe() if orch.oracle else "none (unverified — selection only)"
            console.print(f"oracle: {cur}\nusage: /oracle <cmd>   |   /oracle off")
    elif c == "/protocol":
        if len(parts) > 1 and parts[1] in ("on", "off"):
            orch.protocol = parts[1] == "on"
            state = "on" if orch.protocol else "off"
            console.print(f"protocol -> [bold]{state}[/bold] (affects relay/council)")
        else:
            console.print(f"protocol is [bold]{'on' if orch.protocol else 'off'}[/bold] — usage: /protocol on|off")
    elif c == "/agents":
        for a in agents:
            console.print(f"  [bold {a.color}]{a.label}[/] ({a.name})  model={a.model}")
    elif c == "/skills":
        if not skills:
            console.print("[dim]no skill files found[/dim]")
        for s in skills.values():
            console.print(f"  [bold]{s.name}[/bold] -> {','.join(s.agents)}  [dim]{s.description}[/dim]")
    elif c == "/skill":
        _apply_skill(parts, agents, skills, console)
    elif c == "/clearskills":
        for a in agents:
            a.set_system("")
        console.print("cleared all applied skills")
    elif c == "/reset":
        for a in agents:
            a.clear()
        console.print("conversation history cleared")
    elif c == "/save":
        _save(orch, parts[1] if len(parts) > 1 else "triad_transcript.md", console)
    else:
        console.print(f"[red]unknown command {c}[/red] — try /help")
    return False


def _input(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return "/quit"


async def repl(args) -> None:
    console = Console()
    roster = getattr(args, "roster", "paid")
    providers = {"paid": CORE, "free": EXTRA, "all": CORE + EXTRA}[roster]

    # Make keys available without the user having to `source` anything.
    dotenv.load(DEFAULT_ENV)
    keychain.load_missing([p.env for p in providers])

    agents = build_agents(roster)
    if not agents:
        console.print(f"[yellow]No API keys found for the {roster} roster yet.[/yellow]")
        ans = (await asyncio.to_thread(_input, "Run setup now? [Y/n] ")).strip().lower()
        if ans in ("", "y", "yes"):
            await asyncio.to_thread(run_setup, DEFAULT_ENV, providers, True, False)
            agents = build_agents(roster)
        if not agents:
            console.print("Still no keys. Run [bold]python -m triad setup[/bold] when ready.")
            return

    skills = load_skills(args.skills_dir)
    orch = Orchestrator(agents, skills, console, mode=args.mode, chair=args.chair)
    mem_on = bool(getattr(args, "memory", False))
    orch.history_limit = args.history_limit if args.history_limit else (6 if mem_on else 0)
    if mem_on and not args.vault:
        console.print("[yellow]--memory needs --vault (that's where memory lives) — memory off.[/yellow]")
        mem_on = False
    if args.oracle:
        from .oracle import CommandOracle
        orch.oracle = CommandOracle(args.oracle)

    names = ", ".join(f"[bold {a.color}]{a.label}[/]" for a in agents)
    console.rule("[bold]triad[/bold]")
    console.print(f"agents: {names}   roster: [bold]{roster}[/bold]")
    console.print(f"mode: [bold]{orch.mode}[/bold]   skills loaded: {len(skills)}   (/help for commands)")
    if roster in ("free", "all"):
        console.print(
            "[yellow]free roster:[/yellow] [dim]aggregating free models is only trustworthy with a "
            "verifier (run the code/tests). Without one they make correlated errors and then agree — "
            "use for verifiable tasks; don't judge free council on its own.[/dim]")
    if args.seed_file:
        resume = vault.read_resume(args.seed_file)
        if resume:
            for a in agents:
                a.add_skill(f"## Resume context (from {vault.resume_path(args.seed_file)})\n{resume}")
            console.print(f"resume: loaded {len(resume)} chars from [bold]{vault.resume_path(args.seed_file)}[/bold]")
        else:
            console.print(f"[yellow]resume: nothing read from {args.seed_file}[/yellow]")
    if args.vault:
        vault.open_vault(args.vault, agents, skills)
        console.print(f"vault: live notes → [bold]{args.vault}[/bold] "
                      "(open the folder as an Obsidian vault; Cmd/Ctrl+G for the graph)")
    console.print()

    while True:
        line = (await asyncio.to_thread(_input, f"triad[{orch.mode}]› ")).strip()
        if not line:
            continue
        if line.lower() in ("/keys", "/key"):
            await asyncio.to_thread(run_setup, DEFAULT_ENV, providers, True, False)
            for a in agents:  # apply fixes to keys already in use
                val = os.environ.get(config.resolve(a.name)["key_env"], "")
                if val and val != a.api_key:
                    a.api_key, a._client = val, None
                    console.print(f"updated key for [bold {a.color}]{a.label}[/]")
            _activate_new(agents, console, roster)  # add providers whose keys just appeared (once)
            console.print()
            continue
        if line.startswith("/recall"):
            q = line[len("/recall"):].strip()
            if not args.vault:
                console.print("no vault — start triad with [bold]--vault <dir>[/bold] to build memory")
                continue
            if not q:
                console.print("usage: /recall <query>   (retrieves relevant vault notes; saves re-reading)")
                continue
            from .memory import VaultMemory
            mem = VaultMemory(args.vault)
            block = mem.recall(q)
            if not block:
                console.print("[dim]nothing relevant recalled[/dim]")
            else:
                console.print(block)
                sv = mem.savings(q)
                console.print(f"[dim]memory: ~{sv['recall_tokens']} tokens recalled vs ~{sv['full_tokens']} "
                              f"to re-send the whole vault (~{sv['saved_pct']}% less).[/dim]")
            console.print()
            continue
        if line.startswith("/code "):
            task = line[len("/code "):].strip()
            if not task:
                console.print("usage: /code <what to change>   (uses the current oracle + cwd)")
                continue
            from .coder import EditJob, apply_edits
            from rich.syntax import Syntax
            job = EditJob(agents, oracle=orch.oracle, console=console)
            try:
                result = await job.run(task, ".")
            except Exception as e:
                console.print(f"[red]code error:[/red] {type(e).__name__}: {e}")
                continue
            if not result.diff:
                console.print(f"[red]{result.status}[/red]: {result.detail}")
                continue
            console.rule("[bold]proposed diff[/bold]")
            console.print(Syntax(result.diff, "diff", theme="ansi_dark", word_wrap=True))
            ans = (await asyncio.to_thread(_input, "Apply these edits? [y/N] ")).strip().lower()
            if ans in ("y", "yes"):
                written = apply_edits(".", result.edits or {})
                console.print(f"[green]applied[/green] {len(written)} file(s): {', '.join(written)}")
            else:
                console.print("discarded — repo untouched.")
            console.print()
            continue
        if line.lower() in ("/remember", "/vault"):
            if args.vault:
                n = vault.remember(args.vault, agents, orch)
                console.print(f"remembered → [bold]{args.vault}[/bold] ({n} agent note(s) updated)")
            else:
                console.print("no vault — start triad with [bold]--vault <dir>[/bold]")
            continue
        if line.lower().startswith("/memory"):
            arg = line.split()[1].lower() if len(line.split()) > 1 else ""
            if arg in ("on", "off"):
                if arg == "on" and not args.vault:
                    console.print("[yellow]--memory needs --vault to recall from[/yellow]")
                else:
                    mem_on = arg == "on"
                    if mem_on and not orch.history_limit:
                        orch.history_limit = 6
                    console.print(f"memory -> [bold]{'on' if mem_on else 'off'}[/bold] "
                                  f"(recall relevant vault notes; history capped at {orch.history_limit or '∞'})")
            else:
                console.print(f"memory is [bold]{'on' if mem_on else 'off'}[/bold] — usage: /memory on|off")
            continue
        if line.startswith("/"):
            if _command(line, orch, agents, skills, console):
                break
            continue
        try:
            if mem_on and args.vault:  # rebuild so this turn recalls notes written by earlier turns
                from .memory import VaultMemory
                orch.memory = VaultMemory(args.vault)
                orch.on_evict = lambda ag, dropped: vault.archive_evicted(
                    args.vault, ag.name, ag.label, dropped)
            else:
                orch.memory = None
                orch.on_evict = None
            await orch.dispatch(line)
            if args.vault:
                vault.remember(args.vault, agents, orch)  # live note appears within a second
        except KeyboardInterrupt:
            console.print("\n[yellow]interrupted — turn aborted (Ctrl-C again at the prompt to quit)[/yellow]")
        except Exception as e:
            console.print(f"[red]dispatch error:[/red] {type(e).__name__}: {e}")
        console.print()

    if args.vault:
        vault.remember(args.vault, agents, orch)  # final flush on exit
    console.print("bye 👋")


def _run_setup_cli(argv: List[str]) -> None:
    sp = argparse.ArgumentParser(prog="triad setup",
                                 description="Configure API keys (masked input, live validation).")
    sp.add_argument("--all", action="store_true", help="also configure free-cloud providers")
    sp.add_argument("--no-validate", dest="validate", action="store_false",
                    help="skip the live key check")
    sp.add_argument("--reconfigure", action="store_true", help="re-prompt every key, even ones already set")
    a = sp.parse_args(argv)
    providers = CORE + (EXTRA if a.all else [])
    run_setup(DEFAULT_ENV, providers, validate=a.validate, reconfigure=a.reconfigure)


async def _code_main(args) -> None:
    """Three-head verify-select edit job from the CLI: generate -> verify -> review -> apply."""
    from .coder import EditJob, apply_edits

    console = Console()
    roster = args.roster
    providers = {"paid": CORE, "free": EXTRA, "all": CORE + EXTRA}[roster]
    dotenv.load(DEFAULT_ENV)
    keychain.load_missing([p.env for p in providers])

    agents = build_agents(roster)
    if not agents:
        console.print(f"[yellow]No API keys for the {roster} roster.[/yellow] Run "
                      f"[bold]python -m triad setup{' --all' if roster != 'paid' else ''}[/bold].")
        return

    oracle = None
    if args.oracle:
        from .oracle import CommandOracle
        oracle = CommandOracle(args.oracle)

    task = " ".join(args.task)
    names = ", ".join(f"[bold {a.color}]{a.label}[/]" for a in agents)
    console.print(f"agents: {names}   repo: [bold]{os.path.abspath(args.repo)}[/bold]")
    if not oracle:
        console.print("[yellow]no --oracle: results are UNVERIFIED (selection only). "
                      'Add e.g. --oracle "pytest -q" to gate edits.[/yellow]')

    job = EditJob(agents, oracle=oracle, console=console, rounds=args.rounds)
    result = await job.run(task, args.repo)

    if not result.diff:
        console.print(f"[red]{result.status}[/red]: {result.detail} — nothing to apply.")
        return
    console.rule("[bold]proposed diff[/bold]")
    from rich.syntax import Syntax
    console.print(Syntax(result.diff, "diff", theme="ansi_dark", word_wrap=True))
    tag = "VERIFIED" if result.verified else result.status.upper()
    console.print(f"[bold]{tag}[/bold] — {result.detail}" + (f"  (by {result.winner})" if result.winner else ""))

    if args.yes and result.verified:
        written = apply_edits(args.repo, result.edits or {})
        console.print(f"[green]applied[/green] {len(written)} file(s): {', '.join(written)}")
        return
    ans = (await asyncio.to_thread(_input, "Apply these edits to the repo? [y/N] ")).strip().lower()
    if ans in ("y", "yes"):
        written = apply_edits(args.repo, result.edits or {})
        console.print(f"[green]applied[/green] {len(written)} file(s): {', '.join(written)}")
    else:
        console.print("discarded — repo untouched.")


def _run_code_cli(argv: List[str]) -> None:
    sp = argparse.ArgumentParser(prog="triad code",
                                 description="Three-head verify-select file editing (the dragon's coding mode).")
    sp.add_argument("task", nargs="+", help="what to change, in plain language")
    sp.add_argument("--repo", default=".", help="repository root to edit (default: cwd)")
    sp.add_argument("--oracle", default=None, metavar="CMD",
                    help='pass condition run against each candidate in the sandbox, e.g. --oracle "pytest -q"')
    sp.add_argument("--roster", default="free", choices=["paid", "free", "all"])
    sp.add_argument("--free", action="store_const", const="free", dest="roster", help="(default) free roster")
    sp.add_argument("--rounds", type=int, default=3, help="max critique-revise rounds (default 3)")
    sp.add_argument("--yes", action="store_true", help="apply a VERIFIED result without prompting")
    asyncio.run(_code_main(sp.parse_args(argv)))


async def _bench_main(args) -> None:
    """Stage 3c benchmark: best single free model vs three-head verify-select (+ token savings)."""
    from . import bench

    console = Console()
    if args.tokens:  # measurement only — no agents / no API calls
        from .memory import VaultMemory
        vault_dir = args.vault or ("Claude" if Path("Claude").is_dir() else None)
        if not vault_dir:
            console.print("[yellow]--tokens needs a vault; pass --vault <dir>.[/yellow]")
            return
        mem = VaultMemory(vault_dir)
        if not mem.ready:
            console.print(f"[yellow]no notes found in {vault_dir}.[/yellow]")
            return
        rows = bench.vault_recall_savings(mem)
        growth = bench.history_growth_savings()
        console.print(f"[dim]vault: {mem.stats()}[/dim]")
        console.print(bench.format_token_report(rows, growth))
        console.rule()
        console.print(f"[bold]{bench.token_headline(rows, growth)}[/bold]")
        return

    roster = args.roster
    providers = {"paid": CORE, "free": EXTRA, "all": CORE + EXTRA}[roster]
    dotenv.load(DEFAULT_ENV)
    keychain.load_missing([p.env for p in providers])
    agents = build_agents(roster)
    if len(agents) < 2:
        console.print(f"[yellow]benchmark needs ≥2 heads; the {roster} roster gave {len(agents)}.[/yellow] "
                      f"Run [bold]python -m triad setup --all[/bold] to add free providers.")
        return

    tasks = bench.DEFAULT_TASKS[: args.tasks] if args.tasks else bench.DEFAULT_TASKS

    if args.seeds and args.seeds > 1:   # repeated runs -> mean ± stdev (defensible, not a single bit)
        reports = await bench.run_multi(agents, tasks=tasks, console=console,
                                        rounds=args.rounds, seeds=args.seeds)
        agg = bench.aggregate_runs(reports)
        console.print()
        console.print(bench.format_multi(agg))
        console.rule()
        console.print(f"[bold]{bench.multi_headline(agg)}[/bold]")
        return

    report = await bench.run_benchmark(agents, tasks=tasks, console=console, rounds=args.rounds)
    console.print()
    console.print(bench.format_report(report))
    console.rule()
    console.print(f"[bold]{bench.headline(report)}[/bold]")
    if args.json:
        import json
        data = {"models": report.models, "rounds": report.rounds,
                "best_single": report.best_single(), "select": report.select_rate(),
                "revise": report.revise_rate(),
                "per_model_rate": {m: report.model_rate(m) for m in report.models},
                "tasks": [{"name": o.name, "per_model": o.per_model, "select": o.select,
                           "revise": o.revise} for o in report.outcomes]}
        Path(args.json).write_text(json.dumps(data, indent=2), encoding="utf-8")
        console.print(f"wrote [bold]{args.json}[/bold]")


def _run_bench_cli(argv: List[str]) -> None:
    sp = argparse.ArgumentParser(prog="triad bench",
                                 description="Measure three-head verify-select vs a single free model.")
    sp.add_argument("--roster", default="free", choices=["paid", "free", "all"])
    sp.add_argument("--free", action="store_const", const="free", dest="roster", help="(default) free roster")
    sp.add_argument("--rounds", type=int, default=3, help="max critique-revise rounds (default 3)")
    sp.add_argument("--tasks", type=int, default=0, metavar="N", help="run only the first N tasks (default: all)")
    sp.add_argument("--seeds", type=int, default=1, metavar="N",
                    help="repeat the suite N times and report mean ± stdev (variance; default 1)")
    sp.add_argument("--json", default=None, metavar="FILE", help="also write the results as JSON")
    sp.add_argument("--tokens", action="store_true",
                    help="measure token savings (recall-over-re-read) instead of the pass-rate bench; no API calls")
    sp.add_argument("--vault", default=None, metavar="DIR", help="vault to measure for --tokens (default: Claude)")
    asyncio.run(_bench_main(sp.parse_args(argv)))


def _free_agent(provider: str, model_id: str):
    """Build one specific free brain so the boss can choose it: `--model groq:<id>`."""
    from .agents import GroqAgent, OpenRouterAgent, NIMAgent
    cls = {"groq": GroqAgent, "openrouter": OpenRouterAgent, "open_router": OpenRouterAgent,
           "nim": NIMAgent, "nvidia_nim": NIMAgent, "nvidia": NIMAgent}.get(provider.lower())
    if cls is None:
        return None
    a = cls()
    if model_id:
        a.model = model_id
    return a if a.available else None


def _agent_label(a) -> str:
    return getattr(a, "label", None) or getattr(a, "name", None) or "agent"


def _status_code(exc) -> Optional[int]:
    """Best-effort HTTP status from an OpenAI/httpx-style exception."""
    code = getattr(exc, "status_code", None)
    if code is None:
        code = getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after_seconds(exc) -> Optional[float]:
    """If `exc` is a transient rate-limit (429), the delay to wait before retrying — honoring
    the upstream Retry-After header when present. Returns None for non-retryable errors."""
    blob = str(exc).lower()
    is_429 = _status_code(exc) == 429 or "429" in blob or "rate limit" in blob or "too many requests" in blob
    if not is_429:
        return None
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            hdr = resp.headers.get("retry-after")
            if hdr:
                return float(hdr)
        except (AttributeError, TypeError, ValueError):
            pass
    return 5.0  # sensible default cooldown when the header is absent


def _describe_error(a, exc) -> str:
    """A one-line, caller-readable error: includes the HTTP status so a 429 is unmistakable."""
    name = type(exc).__name__
    msg = (str(exc).strip().splitlines() or [name])[0][:200]
    code = _status_code(exc)
    return f"{_agent_label(a)}: HTTP {code} {name}: {msg}" if code else f"{_agent_label(a)}: {name}: {msg}"


def _spec(a) -> str:
    """Stable cooldown key for an agent: provider:model."""
    return f"{getattr(a, 'name', '?')}:{getattr(a, 'model', '?')}"


def _free_rank_order() -> List[str]:
    env = os.environ.get("TRIAD_FREE_RANK", "").strip()
    if env:
        return [p.strip() for p in env.split(",") if p.strip()]
    from .agents import FREE_RANK
    return list(FREE_RANK)


def _rank_free(agents: List) -> List:
    """Order agents best-first by FREE_RANK; unranked providers keep their place at the end."""
    order = {name: i for i, name in enumerate(_free_rank_order())}
    return sorted(agents, key=lambda a: order.get(getattr(a, "name", ""), len(order)))


async def _try_agent(a, full, system):
    """Run one agent once. Returns (answer|None, error|None, retry_after|None). Records a
    cooldown on a 429 and clears it on a clean answer."""
    try:
        out = await a.complete_raw([{"role": "user", "content": full}], system=system)
        if out and out.strip():
            cooldown.clear(_spec(a))
            return out.strip(), None, None
        return None, f"{_agent_label(a)}: empty response", None
    except Exception as e:
        wait = _retry_after_seconds(e)
        if wait is not None:
            cooldown.mark(_spec(a), wait)
        return None, _describe_error(a, e), wait


async def _run_free_roster(agents: List, full: str, system) -> None:
    """Prioritize the best free model that isn't rate-limited; on a 429 fall straight to the
    next-best; if every model is cooling, wait for the soonest to refresh and retry it. The
    cooldowns persist across calls, so the better model is re-promoted automatically once its
    window passes. On total failure, print the real reason to stderr and exit non-zero."""
    now = time.time()
    ranked = _rank_free(agents)
    ready = [a for a in ranked if not cooldown.is_cooling(_spec(a), now)]
    deferred = [(cooldown.available_at(_spec(a)), a)
                for a in ranked if cooldown.is_cooling(_spec(a), now)]
    last_err = "no free agents available"

    # 1) best-first across the ready models — no blocking, just immediate failover
    for a in ready:
        ans, err, wait = await _try_agent(a, full, system)
        if ans is not None:
            print(ans)
            return
        last_err = err
        if wait is not None:
            print(f"… {_agent_label(a)} rate-limited (429); cooling {wait:.0f}s — trying next",
                  file=sys.stderr)
            deferred.append((time.time() + wait, a))

    # 2) nothing ready answered — wait briefly for the soonest model to refresh and retry it
    deferred.sort(key=lambda t: t[0])
    seen = set()
    for avail, a in deferred:
        if _spec(a) in seen:
            continue
        seen.add(_spec(a))
        wait = max(0.0, avail - time.time())
        if wait > 25.0:
            break                                  # not worth blocking the caller this long
        if wait > 0:
            print(f"… all free models busy; waiting {wait:.0f}s for {_agent_label(a)} to refresh",
                  file=sys.stderr)
            await asyncio.sleep(wait)
        ans, err, _ = await _try_agent(a, full, system)
        if ans is not None:
            print(ans)
            return
        last_err = err

    print(f"(all agents failed) last error: {last_err}", file=sys.stderr)
    sys.exit(1)


async def _ask_main(args) -> None:
    """One-shot worker: hand a task to a free brain, print its answer to stdout. The boss can give it
    EYES (--file: read files into the prompt) and pick the BRAIN (--model provider:id)."""
    providers = {"paid": CORE, "free": EXTRA, "all": CORE + EXTRA}[args.roster]
    dotenv.load(DEFAULT_ENV)
    keychain.load_missing([p.env for p in providers])

    # pick the brain: a specific free model, or fall back to the whole roster (first that answers)
    if getattr(args, "model", None):
        prov, _, mid = args.model.partition(":")
        a = _free_agent(prov.strip(), mid.strip())
        if a is None:
            print(f"(no such free provider/model: {args.model} — use groq:.. | openrouter:.. | nim:..)")
            return
        agents = [a]
    else:
        agents = build_agents(args.roster)
    if not agents:
        print("(no agents available — set free keys with: python -m triad setup --all)")
        return

    system = None
    if args.skill:
        from .skills import load_skills
        sk = load_skills(os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills")).get(args.skill)
        system = sk.body if sk else None

    # give the blind worker eyes: read each --file into the prompt (capped so context stays sane)
    ctx, CAP = "", 40_000
    for fp in getattr(args, "file", []) or []:
        try:
            text = open(os.path.expanduser(fp), encoding="utf-8").read()
            if len(text) > CAP:
                text = text[:CAP] + "\n…(truncated)"
            ctx += f"FILE: {fp}\n```\n{text}\n```\n\n"
        except OSError as e:
            ctx += f"(could not read {fp}: {e})\n\n"
    prompt = " ".join(args.prompt)
    full = f"{ctx}TASK:\n{prompt}" if ctx else prompt

    # Prioritize the best free model that isn't rate-limited; fail over to the next-best on a
    # 429; re-promote the better model once its cooldown refreshes. Surfaces the real error and
    # exits non-zero on total failure, so free-terminal sees the true reason — not a stdout sentinel.
    await _run_free_roster(agents, full, system)


async def _models_main(args) -> None:
    """List/validate/auto-fix model slugs against each provider's live catalog."""
    from . import catalog

    console = Console()
    providers = {"paid": CORE, "free": EXTRA, "all": CORE + EXTRA}[args.roster]
    dotenv.load(DEFAULT_ENV)
    keychain.load_missing([p.env for p in providers])
    agents = build_agents(args.roster)
    if not agents:
        console.print(f"[yellow]No keys for the {args.roster} roster.[/yellow]")
        return

    console.print("[dim]asking each provider for its live model list…[/dim]")
    rows = await catalog.audit(agents)
    from rich.table import Table
    t = Table(title="model catalog")
    for col in ("provider", "current model", "valid?", "suggested", "# served"):
        t.add_column(col)
    changes = []
    for name, cur, valid, suggested, n in rows:
        if n == 0:
            mark = "[dim]not checked (non-OpenAI API or unreachable)[/dim]"
            t.add_row(name, cur, mark, "—", "—")
            continue
        mark = "[green]✓[/green]" if valid else "[red]✗ drifted[/red]"
        sug = suggested or "—"
        t.add_row(name, cur, mark, ("[green]" + sug + "[/green]" if not valid else sug), str(n))
        if args.auto and not valid and suggested:
            changes.append((catalog.model_env(name), suggested, name))
    console.print(t)

    if args.auto and changes:
        for env_key, val, name in changes:
            catalog.upsert_env(DEFAULT_ENV, env_key, val)
            console.print(f"[green]set[/green] {env_key}={val}")
        console.print("[dim]wrote .env — new runs use the validated models.[/dim]")
    elif not args.auto and any(not v and n for (_, _, v, _, n) in rows):
        console.print("[dim]run [bold]triad models --auto[/bold] to write the suggested valid slugs to .env.[/dim]")


def _run_serve_cli(argv: List[str]) -> None:
    sp = argparse.ArgumentParser(prog="triad serve",
                                 description="Open triad in a local browser window.")
    sp.add_argument("--roster", default="paid", choices=["paid", "free", "all"])
    sp.add_argument("--free", action="store_const", const="free", dest="roster",
                    help="shorthand for --roster free")
    sp.add_argument("--port", type=int, default=8770)
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--no-open", dest="open_browser", action="store_false",
                    help="don't auto-open the browser")
    a = sp.parse_args(argv)
    from . import web
    web.serve(roster=a.roster, host=a.host, port=a.port, open_browser=a.open_browser)


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "setup":
        _run_setup_cli(argv[1:])
        return
    if argv and argv[0] == "serve":
        _run_serve_cli(argv[1:])
        return
    if argv and argv[0] == "code":
        _run_code_cli(argv[1:])
        return
    if argv and argv[0] == "bench":
        _run_bench_cli(argv[1:])
        return
    if argv and argv[0] == "ask":
        sp = argparse.ArgumentParser(prog="triad ask",
                                     description="One-shot free-model answer to stdout — the 'free worker' a "
                                                 "Fighty/relay orchestrator can call like any other CLI.")
        sp.add_argument("prompt", nargs="+", help="the task/question")
        sp.add_argument("--roster", default="free", choices=["paid", "free", "all"])
        sp.add_argument("--skill", default=None, help="apply a skill file (by name) as the system prompt")
        sp.add_argument("--file", action="append", default=[], metavar="PATH",
                        help="give the worker EYES: read this file into its prompt (repeatable)")
        sp.add_argument("--model", default=None, metavar="PROVIDER:ID",
                        help="pick the BRAIN, e.g. groq:llama-3.3-70b-versatile or "
                             "openrouter:openai/gpt-oss-120b:free  (run 'triad models --roster free' to list)")
        a = sp.parse_args(argv[1:])
        asyncio.run(_ask_main(a))
        return
    if argv and argv[0] == "models":
        sp = argparse.ArgumentParser(prog="triad models",
                                     description="Ask each provider what it serves; validate/auto-fix your model slugs.")
        sp.add_argument("--roster", default="all", choices=["paid", "free", "all"])
        sp.add_argument("--auto", action="store_true", help="write a valid model to .env for any drifted/invalid slug")
        a = sp.parse_args(argv[1:])
        asyncio.run(_models_main(a))
        return
    if argv and argv[0] == "cass":
        sp = argparse.ArgumentParser(prog="triad cass",
                                     description="Serve the astronaut game with Cass voiced by a free model.")
        sp.add_argument("--game", default=".", help="folder containing the game's index.html")
        sp.add_argument("--roster", default="free", choices=["paid", "free", "all"])
        sp.add_argument("--port", type=int, default=8095)
        sp.add_argument("--no-open", dest="open_browser", action="store_false")
        a = sp.parse_args(argv[1:])
        from . import cass
        cass.serve(a.game, roster=a.roster, port=a.port, open_browser=a.open_browser)
        return

    p = argparse.ArgumentParser(prog="triad",
                                description="Talk to ChatGPT, Claude, and Gemini at once.")
    p.add_argument("--mode", default="parallel", choices=["parallel", "relay", "council", "verify"])
    p.add_argument("--oracle", default=None, metavar="CMD",
                   help='verify-mode pass condition, run against each candidate in the sandbox, '
                        'e.g. --oracle "pytest -q"')
    p.add_argument("--roster", default="paid", choices=["paid", "free", "all"],
                   help="paid=OpenAI/Anthropic/Gemini, free=Groq/OpenRouter/NIM, all=both")
    p.add_argument("--free", action="store_const", const="free", dest="roster",
                   help="shorthand for --roster free")
    p.add_argument("--web", action="store_true", help="open triad in a local browser instead of the terminal")
    p.add_argument("--port", type=int, default=8770, help="port for --web (default 8770)")
    p.add_argument("--skills-dir", default=os.path.join(os.path.dirname(os.path.dirname(__file__)), "skills"))
    p.add_argument("--chair", default=None, help="agent name to synthesize in council mode (default: first)")
    p.add_argument("--vault", default=None, metavar="DIR",
                   help="write a live Obsidian vault of the session to DIR (notes per agent + topics + index)")
    p.add_argument("--seed-file", dest="seed_file", default=None, metavar="DIR|FILE",
                   help="load a vault's index.md (or any markdown file) back as resume context")
    p.add_argument("--memory", action="store_true",
                   help="recall relevant vault notes each turn instead of re-sending all history (needs --vault)")
    p.add_argument("--history-limit", dest="history_limit", type=int, default=0, metavar="N",
                   help="keep only the last N exchanges per agent (older memory lives in the vault)")
    args = p.parse_args()
    if args.web:
        from . import web
        web.serve(roster=args.roster, port=args.port)
        return
    asyncio.run(repl(args))


if __name__ == "__main__":
    main()
