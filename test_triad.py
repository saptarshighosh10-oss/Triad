"""Offline test harness for triad — no API keys, no network, no spend.

Drives the real orchestrator/UI with mock agents and exercises every pure-logic
module with real inputs. Run: .venv/bin/python test_triad.py
"""
from __future__ import annotations

import asyncio
import io
import os
import tempfile
from pathlib import Path

from rich.console import Console

from triad.agents import Agent, build_agents
from triad.orchestrator import Orchestrator
from triad import dotenv, protocol
from triad.skills import load_skills
from triad.setup_keys import mask, CORE

PASS, FAIL = [], []

def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗ FAIL"
    print(f"  {mark}  {name}" + (f"   [{detail}]" if detail and not cond else ""))


# --------------------------------------------------------------- mock agent
class MockAgent(Agent):
    """Agent that records what it was asked and streams a scripted reply."""
    def __init__(self, name, label, reply="ok"):
        self.name, self.label, self.color = name, label, "white"
        self.model, self.api_key = "mock-model", "x"
        self.system_prompt, self.history, self._client = "", [], None
        self.reply = reply
        self.calls = []  # [(messages, system), ...]

    async def _provider_stream(self, messages, system):
        self.calls.append(([dict(m) for m in messages], system))
        r = self.reply(self) if callable(self.reply) else self.reply
        # stream in small chunks to simulate real deltas
        for i in range(0, len(r), 4):
            yield r[i:i + 4]
        if not r:
            return


def quiet_console():
    return Console(file=io.StringIO(), force_terminal=False, width=120)


# --------------------------------------------------------------- tests
def test_protocol():
    print("\n[protocol]")
    good = ("@goal build a parser\n"
            "@find\n- handles quotes\n- 600 perms\n* bullet star\n"
            "@conf 0.8\n@next claude reviews")
    n = protocol.parse_note(good)
    check("parse @goal", n.goal == "build a parser", n.goal)
    check("parse @find list", n.find == ["handles quotes", "600 perms", "bullet star"], str(n.find))
    check("parse @conf", n.conf == "0.8", n.conf)
    check("parse @next", n.nxt == "claude reviews", n.nxt)

    block = protocol.compact_block(good, "Claude")
    check("compact keeps findings", "handles quotes" in block and "@conf 0.8" in block)
    check("compact has label header", block.startswith("## Claude"))

    off = "I think the answer is 42 because reasons " * 50
    fb = protocol.compact_block(off, "Gemini", max_chars=100)
    check("off-format falls back to trim", fb.startswith("## Gemini") and len(fb) < 200, f"len={len(fb)}")
    check("est_tokens positive", protocol.est_tokens("abcd" * 10) == 10)
    check("est_tokens floor", protocol.est_tokens("") == 1)

    rs = protocol.RefStore()
    ref = rs.put("relay/1", "full text")
    check("refstore put returns pointer", ref == "ref:relay/1")
    check("refstore get", rs.get("relay/1") == "full text")
    check("refstore miss is empty", rs.get("nope") == "")


def test_dotenv():
    print("\n[dotenv]")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / ".env"
        dotenv.write_var(p, "OPENAI_API_KEY", "sk-123")
        dotenv.write_var(p, "ANTHROPIC_API_KEY", "sk-ant-xyz")
        dotenv.write_var(p, "OPENAI_API_KEY", "sk-updated")  # update in place
        parsed = dotenv.parse(p)
        check("write+parse value", parsed.get("ANTHROPIC_API_KEY") == "sk-ant-xyz")
        check("update in place (no dup)", parsed.get("OPENAI_API_KEY") == "sk-updated")
        check("no duplicate keys", p.read_text().count("OPENAI_API_KEY") == 1)
        mode = oct(p.stat().st_mode)[-3:]
        check("chmod 600", mode == "600", mode)

        # quote styles + export prefix
        p2 = Path(d) / "mixed.env"
        p2.write_text('export FOO="bar"\nBAZ=qux\n# comment\nQUOTED=\'val\'\n')
        m = dotenv.parse(p2)
        check("export+dquote", m.get("FOO") == "bar")
        check("plain", m.get("BAZ") == "qux")
        check("squote", m.get("QUOTED") == "val")
        check("comment skipped", "# comment" not in m)

        # gitignore
        added = dotenv.ensure_gitignore(p)
        check("gitignore added", added and ".env" in (Path(d) / ".gitignore").read_text())
        added2 = dotenv.ensure_gitignore(p)
        check("gitignore idempotent", added2 is False)

        # load into environ
        os.environ.pop("BAZ", None)
        dotenv.load(p2)
        check("load into environ", os.environ.get("BAZ") == "qux")
        os.environ.pop("BAZ", None)


def test_skills():
    print("\n[skills]")
    sk = load_skills("skills")
    check("loaded 7 skills", len(sk) == 7, str(len(sk)))
    check("code-reviewer targets gemini", sk.get("code-reviewer") and sk["code-reviewer"].agents == ["gemini"])
    check("planner targets all", sk.get("planner") and sk["planner"].agents == ["all"])
    check("body captured", "rigorous code reviewer" in sk["code-reviewer"].body)
    check("description captured", "bugs" in sk["code-reviewer"].description.lower())
    check("missing dir -> empty", load_skills("/no/such/dir") == {})


def test_mask():
    print("\n[mask]")
    check("mask long key", mask("sk-1234567890abcd") == "sk-…abcd")
    check("mask short key", set(mask("12345")) == {"•"})
    check("mask empty", mask("") == "(not set)")


def test_build_agents_no_keys():
    print("\n[build_agents]")
    saved = {p.env: os.environ.pop(p.env, None) for p in CORE}
    try:
        check("no keys -> no agents", build_agents() == [])
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


def mk_three():
    return [MockAgent("chatgpt", "ChatGPT", "A-says"),
            MockAgent("claude", "Claude", "B-says"),
            MockAgent("gemini", "Gemini", "C-says")]


def test_parallel():
    print("\n[orchestrator: parallel]")
    agents = mk_three()
    orch = Orchestrator(agents, {}, quiet_console(), mode="parallel")
    asyncio.run(orch.dispatch("hello"))
    check("all 3 got the task", all(a.calls and a.calls[0][0][-1]["content"] == "hello" for a in agents))
    check("parallel records history", all(len(a.history) == 2 for a in agents),
          str([len(a.history) for a in agents]))
    check("history holds reply", agents[0].history[1]["content"] == "A-says")
    check("transcript captures parallel turn",
          len(orch.transcript) == 1 and "A-says" in orch.transcript[0] and "[parallel]" in orch.transcript[0])


def test_relay():
    print("\n[orchestrator: relay]")
    agents = mk_three()
    orch = Orchestrator(agents, {}, quiet_console(), mode="relay")
    asyncio.run(orch.dispatch("design X"))
    # agent 0 sees only the bare task; later agents see prior work
    check("relay step1 gets bare task", agents[0].calls[0][0][0]["content"] == "design X")
    p2 = agents[1].calls[0][0][0]["content"]
    check("relay step2 sees task", "design X" in p2)
    check("relay step2 sees prior work", "A-says" in p2 and "ChatGPT" in p2, p2[:80])
    p3 = agents[2].calls[0][0][0]["content"]
    check("relay step3 sees both priors", "A-says" in p3 and "B-says" in p3)
    check("transcript captures relay (was lost before fix)",
          orch.transcript and all(s in orch.transcript[0] for s in ("A-says", "B-says", "C-says")))


def test_relay_protocol():
    print("\n[orchestrator: relay + protocol]")
    proto_reply = ("@goal solve\n@find\n- key point one\n@conf 0.9\n@next done")
    agents = [MockAgent("chatgpt", "ChatGPT", proto_reply),
              MockAgent("claude", "Claude", proto_reply),
              MockAgent("gemini", "Gemini", proto_reply)]
    console = quiet_console()
    orch = Orchestrator(agents, {}, console, mode="relay")
    orch.protocol = True
    asyncio.run(orch.dispatch("task"))
    check("step1 gets protocol instruction", "compact handoff format" in agents[0].calls[0][0][0]["content"])
    p2 = agents[1].calls[0][0][0]["content"]
    check("step2 passes compact digest not raw", "@find" in p2 and "key point one" in p2)
    out = console.file.getvalue()
    check("prints savings line", "protocol:" in out and "less passed" in out, out[-120:])


def test_council():
    print("\n[orchestrator: council]")
    agents = mk_three()
    console = quiet_console()
    chair = "claude"
    orch = Orchestrator(agents, {}, console, mode="council", chair=chair)
    asyncio.run(orch.dispatch("hard question"))
    # round 1: everyone answered once; round 2: chair called again -> 2 calls
    counts = {a.name: len(a.calls) for a in agents}
    check("chair called twice", counts["claude"] == 2, str(counts))
    check("non-chair called once", counts["chatgpt"] == 1 and counts["gemini"] == 1, str(counts))
    synth = agents[1].calls[1][0][0]["content"]
    check("synthesis sees all responses", "A-says" in synth and "C-says" in synth)
    check("transcript captures council + synthesis (was lost before fix)",
          orch.transcript and "synthesis" in orch.transcript[0] and "A-says" in orch.transcript[0])


def test_one_agent_error_isolated():
    print("\n[resilience: one agent errors]")
    def boom(self):
        raise RuntimeError("provider exploded")
    agents = [MockAgent("chatgpt", "ChatGPT", "fine"),
              MockAgent("claude", "Claude", boom),
              MockAgent("gemini", "Gemini", "also fine")]
    console = quiet_console()
    orch = Orchestrator(agents, {}, console, mode="parallel")
    asyncio.run(orch.dispatch("go"))  # must not raise
    out = console.file.getvalue()
    check("error surfaced, not crashed", "error" in out.lower())
    check("healthy agents still recorded history", len(agents[0].history) == 2 and len(agents[2].history) == 2)


def test_activate_new_no_dup():
    print("\n[cli /keys: activate new provider once — FIX A]")
    from triad.cli import _activate_new
    saved = {p.env: os.environ.pop(p.env, None) for p in CORE}
    try:
        os.environ["ANTHROPIC_API_KEY"] = "x"   # makes Claude available
        agents = []                              # orch.agents would alias this same list
        _activate_new(agents, quiet_console())
        check("new provider added once", [a.name for a in agents] == ["claude"], str([a.name for a in agents]))
        _activate_new(agents, quiet_console())   # calling again must not duplicate
        check("idempotent — no double add", [a.name for a in agents].count("claude") == 1)
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def test_save_all_modes():
    print("\n[/save captures every mode — FIX B]")
    from triad.cli import _save
    for mode in ("parallel", "relay", "council"):
        agents = mk_three()
        orch = Orchestrator(agents, {}, quiet_console(), mode=mode)
        asyncio.run(orch.dispatch("the question"))
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "t.md"
            _save(orch, str(f), quiet_console())
            body = f.read_text()
        check(f"/save({mode}) writes agent output", "A-says" in body and "the question" in body,
              f"len={len(body)}")
    # empty session -> friendly no-op, no file written
    orch = Orchestrator(mk_three(), {}, quiet_console(), mode="parallel")
    con = quiet_console()
    _save(orch, "/tmp/should_not_exist_triad.md", con)
    check("empty /save is a no-op", "nothing to save" in con.file.getvalue()
          and not Path("/tmp/should_not_exist_triad.md").exists())


def test_parallel_cleanup_on_cancel():
    print("\n[resilience: Ctrl-C cancels child streams — FIX C]")
    from triad.ui import live_parallel

    class HangAgent(MockAgent):
        async def _provider_stream(self, messages, system):
            while True:                      # a stream that never finishes
                await asyncio.sleep(0.01)
                yield "."

    async def scenario():
        agents = [HangAgent("a", "A"), HangAgent("b", "B")]
        outer = asyncio.create_task(live_parallel(quiet_console(), agents, lambda a: a.stream("x")))
        await asyncio.sleep(0.12)            # let the child provider tasks spin up
        outer.cancel()                       # simulate the KeyboardInterrupt unwind
        try:
            await outer
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0.05)            # let cleanup settle
        return [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    leftover = asyncio.run(scenario())
    check("no orphaned provider tasks after cancel", len(leftover) == 0, f"{len(leftover)} leftover")


def test_free_roster():
    print("\n[free roster + base_url + fcc Claude slot — STAGE 1]")
    from triad.agents import build_agents, GroqAgent, ClaudeAgent
    all_keys = ["OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY",
                "GROQ_API_KEY", "OPENROUTER_API_KEY", "NVIDIA_NIM_API_KEY"]
    saved = {k: os.environ.pop(k, None) for k in all_keys}
    saved_burl = os.environ.pop("TRIAD_CLAUDE_BASE_URL", None)
    try:
        os.environ["GROQ_API_KEY"] = "g"
        os.environ["OPENROUTER_API_KEY"] = "o"
        os.environ["NVIDIA_NIM_API_KEY"] = "n"
        free = build_agents("free")
        check("free roster = groq/openrouter/nim", [a.name for a in free] == ["groq", "openrouter", "nim"],
              str([a.name for a in free]))
        check("paid roster empty without paid keys", build_agents("paid") == [])
        check("all roster = the free 3 (no paid keys here)",
              [a.name for a in build_agents("all")] == ["groq", "openrouter", "nim"])

        models = " ".join(a.model.lower() for a in free)
        check("default models span 3 lineages (llama/gpt-oss/qwen)",
              all(x in models for x in ("llama", "gpt-oss", "qwen")), models)

        g = GroqAgent()
        check("groq base_url from config", g.base_url == "https://api.groq.com/openai/v1")
        check("OpenAI client built with base_url", "api.groq.com" in str(g._obj().base_url))

        # root cause of Bug E: an override set AFTER import now applies (lazy resolve, no
        # dependence on import order). The old module-level snapshot failed this.
        os.environ["TRIAD_GROQ_MODEL"] = "late-override-xyz"
        check("lazy config: env override after import applies", GroqAgent().model == "late-override-xyz")
        os.environ.pop("TRIAD_GROQ_MODEL", None)

        os.environ.pop("GROQ_API_KEY", None)
        check("cloud free agent unavailable without key", GroqAgent().available is False)

        # fcc free-Claude slot: TRIAD_CLAUDE_BASE_URL (local) makes Claude available with NO key.
        # Set via env (not a dict patch) so this exercises the real lazy-resolve path.
        os.environ["TRIAD_CLAUDE_BASE_URL"] = "http://localhost:8082"
        c = ClaudeAgent()
        check("fcc Claude available via local base_url (no key)", c.available is True)
        check("base_url resolved lazily from env", c.base_url == "http://localhost:8082")
        check("Anthropic client built against fcc base_url", "localhost:8082" in str(c._obj().base_url))
        os.environ.pop("TRIAD_CLAUDE_BASE_URL", None)
        check("claude unavailable without key or base_url", ClaudeAgent().available is False)
    finally:
        os.environ.pop("TRIAD_CLAUDE_BASE_URL", None)
        if saved_burl is not None:
            os.environ["TRIAD_CLAUDE_BASE_URL"] = saved_burl
        for k in all_keys:
            os.environ.pop(k, None)
            if saved[k] is not None:
                os.environ[k] = saved[k]


def test_vault():
    print("\n[obsidian vault — OBSIDIAN]")
    import tempfile
    from triad import vault
    with tempfile.TemporaryDirectory() as d:
        vd = Path(d)
        agents = [MockAgent("chatgpt", "ChatGPT", "use generate-verify-select for correctness"),
                  MockAgent("claude", "Claude", "oracle independence is the crux"),
                  MockAgent("gemini", "Gemini", "cross-provider decorrelation keeps it honest")]
        vault.open_vault(d, agents, {})
        check("index.md created", (vd / "index.md").exists())
        check("8 topic notes seeded", len(list((vd / "topics").glob("*.md"))) == 8)
        check("one living note per agent",
              sorted(p.stem for p in (vd / "agents").glob("*.md")) == ["chatgpt", "claude", "gemini"])
        check("index links the agents + topics",
              all(s in (vd / "index.md").read_text() for s in ("[[claude]]", "[[generate-verify-select]]")))

        orch = Orchestrator(agents, {}, quiet_console(), mode="parallel")
        asyncio.run(orch.dispatch("how do we trust the free roster?"))
        check("remember updates every agent note", vault.remember(d, agents, orch) == 3)
        check("turn summary is wikilinked", "[[generate-verify-select]]" in (vd / "agents" / "chatgpt.md").read_text())
        check("remember is idempotent per turn (no dup)", vault.remember(d, agents, orch) == 0)

        cp = vault.checkpoint(d, "cp", "shipping [[generate-verify-select]] next")
        check("checkpoint note created", cp.exists())
        check("index relinks the checkpoint", cp.stem in (vd / "index.md").read_text())

        total, orphans = vault.check_links(d)
        check("no orphan wikilinks (every edge resolves)", orphans == [], str(orphans[:3]))
        check("graph has real edges", total > 30, f"{total} links")

        check("read_resume returns index.md", "vault index" in vault.read_resume(d))
        check("resume_path resolves a dir to index.md", vault.resume_path(d).name == "index.md")


def test_sandbox():
    print("\n[execution sandbox — STAGE 2 (untrusted code)]")
    from triad.sandbox import Sandbox
    sb = Sandbox()
    check("tier is a known tier", sb.tier in ("docker", "macos-seatbelt", "subprocess"), sb.tier)
    check("isolation note is non-empty", bool(sb.note))
    check("fully_isolated only when docker", sb.fully_isolated == (sb.tier == "docker"))

    r = sb.run({"h.py": "print('hi')"}, ["python3", "h.py"])
    check("runs code (rc 0, stdout captured)", r.ok and r.stdout.strip() == "hi", f"rc={r.returncode}")
    check("propagates exit code", sb.run({"b.py": "raise SystemExit(3)"}, ["python3", "b.py"]).returncode == 3)

    r = sb.run({"s.py": "import time; time.sleep(30)"}, ["python3", "s.py"], timeout=2)
    check("enforces wall-clock timeout", r.timed_out and r.returncode == 124)

    os.environ["TRIAD_SBX_SECRET"] = "leak"
    r = sb.run({"e.py": "import os; print(os.environ.get('TRIAD_SBX_SECRET', 'NONE'))"}, ["python3", "e.py"])
    os.environ.pop("TRIAD_SBX_SECRET", None)
    check("scrubs parent env (no secret leaks in)", r.stdout.strip() == "NONE", r.stdout.strip())

    if sb.network_blocked:
        net = ("import socket\ntry:\n socket.create_connection(('1.1.1.1', 53), timeout=3); print('NET_OK')\n"
               "except Exception: print('NET_BLOCKED')")
        r = sb.run({"n.py": net}, ["python3", "n.py"], timeout=6)
        check("no-net claim is real on this tier (not best-effort)", "NET_OK" not in r.stdout, r.stdout.strip())
    else:
        check("subprocess tier honestly admits no network block", sb.network_blocked is False)

    # fail-LOUD, not fail-open: a seatbelt that stops enforcing must degrade to subprocess + warn,
    # never keep claiming network_blocked while the network is actually open (sandbox-exec is deprecated).
    import sys as _sys, shutil as _shutil, triad.sandbox as sbx
    if _sys.platform == "darwin" and _shutil.which("sandbox-exec"):
        saved_cache, saved_probe = sbx._detect_cache, sbx._seatbelt_blocks_network
        try:
            sbx._detect_cache = None
            sbx._seatbelt_blocks_network = lambda: False   # simulate a neutered/broken sandbox-exec
            degraded = sbx.Sandbox()
            check("broken seatbelt degrades to subprocess (fail-loud)", degraded.tier == "subprocess", degraded.tier)
            check("degraded sandbox does NOT claim network blocked", degraded.network_blocked is False)
            check("degrade reason surfaced in the note", "did NOT block" in degraded.note)
        finally:
            sbx._seatbelt_blocks_network, sbx._detect_cache = saved_probe, saved_cache


def test_verify():
    print("\n[verify mode: generate-verify-select — STAGE 2]")
    from triad.oracle import CommandOracle, AbsentOracle, extract_code
    from triad.sandbox import Sandbox

    check("extract_code pulls the fenced block", extract_code("pre\n```python\nx = 1\n```\npost") == "x = 1")
    check("extract_code falls back to raw text", extract_code("no fence here") == "no fence here")
    check("CommandOracle is independent", CommandOracle("true").independent is True)
    check("AbsentOracle is NOT independent (selection only)", AbsentOracle().independent is False)
    check("AbsentOracle never returns pass", AbsentOracle().check("```\nx\n```", None).status == "unverified")

    GOOD = "```python\ndef add(a, b):\n    return a + b\n```"
    BAD = "```python\ndef add(a, b):\n    return a - b\n```"
    oracle = CommandOracle('python3 -c "import solution; assert solution.add(2, 3) == 5"')
    sb = Sandbox()
    check("oracle PASSES a correct candidate (real sandbox run)", oracle.check(GOOD, sb).status == "pass",
          oracle.check(GOOD, sb).detail)
    check("oracle FAILS a wrong candidate", oracle.check(BAD, sb).status == "fail")

    def reviser():
        def reply(self):
            last = self.calls[-1][0][-1]["content"] if self.calls else ""
            return GOOD if "FAILED" in last else BAD     # fix only after seeing a failure
        return reply

    # round-1 pass: one candidate is correct immediately -> selected, VERIFIED
    agents = [MockAgent("chatgpt", "ChatGPT", BAD), MockAgent("claude", "Claude", GOOD),
              MockAgent("gemini", "Gemini", BAD)]
    orch = Orchestrator(agents, {}, quiet_console(), mode="verify"); orch.oracle = oracle
    asyncio.run(orch.dispatch("write add(a,b)"))
    check("verify selects a passer (VERIFIED)", "PASSED — Claude" in "\n".join(orch.transcript))

    # critique-revise: all fail round 1, the reviser fixes it round 2 -> PASSED
    agents2 = [MockAgent("chatgpt", "ChatGPT", reviser()), MockAgent("claude", "Claude", BAD),
               MockAgent("gemini", "Gemini", BAD)]
    orch2 = Orchestrator(agents2, {}, quiet_console(), mode="verify"); orch2.oracle = oracle
    asyncio.run(orch2.dispatch("write add(a,b)"))
    check("critique-revise: fail round 1 -> pass round 2", "PASSED — ChatGPT round 2" in "\n".join(orch2.transcript),
          "\n".join(orch2.transcript)[-120:])

    # all fail within the bound -> honest FAILED, never a fabricated pass
    agents3 = [MockAgent("chatgpt", "ChatGPT", BAD), MockAgent("claude", "Claude", BAD)]
    orch3 = Orchestrator(agents3, {}, quiet_console(), mode="verify"); orch3.oracle = oracle
    asyncio.run(orch3.dispatch("write add"))
    rec3 = "\n".join(orch3.transcript)
    check("all-fail ends honestly (FAILED, no fake pass)", "FAILED — 0/2" in rec3 and "PASSED" not in rec3)

    # no oracle -> UNVERIFIED, selection only, never a pass
    orch4 = Orchestrator(mk_three(), {}, quiet_console(), mode="verify")  # oracle stays None
    asyncio.run(orch4.dispatch("anything"))
    rec4 = "\n".join(orch4.transcript)
    check("no-oracle verify is UNVERIFIED selection only", "UNVERIFIED — selection only" in rec4 and "PASSED" not in rec4)


def test_sandbox_node():
    print("\n[sandbox: JS/TS runtime support — verified edits on real TS repos]")
    import shutil
    from triad.sandbox import Sandbox, _extra_bin_dirs
    if not shutil.which("node"):
        check("node not installed — JS/TS oracle test skipped", True)
        return
    check("node's dir is added to the sandbox's extra bin dirs",
          str(Path(shutil.which("node")).parent) in _extra_bin_dirs())
    sb = Sandbox()
    files = {"t.ts": "export const inc = (x: number): number => x + 1;\n",
             "run.ts": "import {inc} from './t.ts';\nif (inc(2) !== 3) process.exit(1);\nconsole.log('ok');\n"}
    r = sb.run(files, ["/bin/sh", "-c", "node --experimental-strip-types run.ts"], timeout=30)
    check("sandbox runs a TypeScript oracle (node reachable, types stripped)", r.ok,
          f"rc={r.returncode} {(r.stderr or '')[-120:]}")


def test_coder():
    print("\n[coder: three-head verify-select file editing — STAGE 3]")
    from triad.coder import (read_repo, parse_edits, build_diff, apply_edits, _safe_relpath,
                             EditJob)
    from triad.oracle import CommandOracle

    # ---- pure helpers ----
    reply = ("Here you go:\n"
             "FILE: pkg/calc.py\n```python\ndef add(a, b):\n    return a + b\n```\n"
             "FILE: notes.txt\n```\nhello\n```\n")
    edits = parse_edits(reply)
    check("parse_edits finds both files", set(edits) == {"pkg/calc.py", "notes.txt"})
    check("parse_edits keeps full file body", edits["pkg/calc.py"].strip().endswith("return a + b"))
    check("parse_edits ignores prose outside blocks", "Here you go" not in "".join(edits.values()))

    check("path guard rejects absolute", _safe_relpath("/etc/passwd") is None)
    check("path guard rejects .. escape", _safe_relpath("../../x") is None)
    check("path guard strips ./", _safe_relpath("./a/b.py") == "a/b.py")
    check("parse_edits drops unsafe paths",
          parse_edits("FILE: ../evil.py\n```\nx\n```\n") == {})

    diff = build_diff({"a.py": "x = 1\n"}, {"a.py": "x = 2\n", "new.py": "y\n"})
    check("build_diff shows a change", "-x = 1" in diff and "+x = 2" in diff)
    check("build_diff shows a new file", "new.py" in diff and "/dev/null" in diff)
    check("build_diff skips unchanged files", build_diff({"a": "1"}, {"a": "1"}) == "")

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "keep.py").write_text("print('hi')\n")
        (root / ".env").write_text("SECRET=should-not-be-read\n")
        sub = root / "sub"; sub.mkdir()
        (sub / "mod.py").write_text("v = 1\n")
        (root / "__pycache__").mkdir(); (root / "__pycache__" / "x.pyc").write_text("junk")
        files = read_repo(root)
        check("read_repo collects source files", "keep.py" in files and "sub/mod.py" in files)
        check("read_repo skips dotfiles (.env stays out of prompts)", ".env" not in files)
        check("read_repo skips __pycache__", not any("pycache" in p for p in files))

        written = apply_edits(root, {"sub/mod.py": "v = 2\n", "added.py": "z = 3\n"})
        check("apply_edits writes changed + new files", set(written) == {"sub/mod.py", "added.py"})
        check("apply_edits actually edited disk", (sub / "mod.py").read_text() == "v = 2\n")
        check("apply_edits refuses traversal", apply_edits(root, {"../boom.py": "x"}) == [])

    # ---- relevance-based repo context (recall-over-re-read for the coder) ----
    from triad.coder import build_context
    small = {"a.py": "x = 1\n", "b.py": "y = 2\n"}
    ctx, inc = build_context("change a", small, max_bytes=10_000)
    check("small repo: all files included in full", set(inc) == {"a.py", "b.py"})

    big = {f"mod{i}.py": ("# filler unrelated content line\n" * 200) for i in range(8)}
    big["payments.py"] = "def charge(amount):\n    return amount  # the relevant file\n"
    ctx2, inc2 = build_context("fix the charge function in payments.py", big, max_bytes=2_000)
    check("big repo: relevant file included in full", "payments.py" in inc2 and "def charge" in ctx2)
    check("big repo: most files dropped to a name-only manifest", len(inc2) < len(big))
    check("big repo: omitted files still listed by name", "OTHER FILES" in ctx2 and "mod0.py" in ctx2)
    check("big repo: context respects the byte budget", len(ctx2) < 2_000 + 600)

    # ---- the engine, end-to-end with mock heads + the real sandbox ----
    GOOD = "FILE: solution.py\n```python\ndef add(a, b):\n    return a + b\n```\n"
    BAD = "FILE: solution.py\n```python\ndef add(a, b):\n    return a - b\n```\n"
    oracle = CommandOracle('python3 -c "import solution; assert solution.add(2,3)==5"')

    def reviser():
        def reply(self):
            last = self.calls[-1][0][-1]["content"] if self.calls else ""
            return GOOD if "FAILED" in last else BAD
        return reply

    with tempfile.TemporaryDirectory() as d:
        # round-1 pass: one head correct immediately -> VERIFIED, that head wins
        agents = [MockAgent("groq", "Groq", BAD), MockAgent("openrouter", "OpenRouter", GOOD),
                  MockAgent("nim", "NIM", BAD)]
        job = EditJob(agents, oracle=oracle, console=quiet_console())
        res = asyncio.run(job.run("write add(a,b)", d))
        check("coder verifies a passing candidate", res.verified and res.winner == "OpenRouter",
              f"{res.status}/{res.winner}")
        check("coder returns an applyable diff", "def add" in res.diff and bool(res.edits))

        # critique-revise: all fail round 1, reviser fixes round 2 -> VERIFIED round 2
        agents2 = [MockAgent("groq", "Groq", reviser()), MockAgent("openrouter", "OpenRouter", BAD)]
        job2 = EditJob(agents2, oracle=oracle, console=quiet_console(), rounds=3)
        res2 = asyncio.run(job2.run("write add(a,b)", d))
        check("coder critique-revise recovers round 2", res2.verified and res2.rounds == 2,
              f"{res2.status} r{res2.rounds}")

        # all fail within bound -> honest FAILED, no diff, repo untouched
        agents3 = [MockAgent("groq", "Groq", BAD), MockAgent("openrouter", "OpenRouter", BAD)]
        job3 = EditJob(agents3, oracle=oracle, console=quiet_console(), rounds=2)
        res3 = asyncio.run(job3.run("write add", d))
        check("coder all-fail is honest (no fake pass)", res3.status == "failed" and not res3.diff)

        # no oracle -> UNVERIFIED selection only, never 'verified'
        job4 = EditJob([MockAgent("groq", "Groq", GOOD)], oracle=None, console=quiet_console())
        res4 = asyncio.run(job4.run("write add", d))
        check("coder no-oracle is UNVERIFIED selection only", res4.status == "unverified" and not res4.verified)


def test_bench():
    print("\n[bench: three-head verify-select vs single model — STAGE 3c]")
    from triad.bench import Task, run_benchmark, format_report, headline, BenchReport, TaskOutcome

    # Two heads, two tasks, complementary skills: A solves add, B solves mul. Neither alone clears
    # both (50%); their oracle-checked union does (100%). This is the decorrelation win, in miniature.
    A = MockAgent("groq", "Groq", "FILE: solution.py\n```python\ndef add(a, b):\n    return a + b\n```\n")
    B = MockAgent("openrouter", "OpenRouter", "FILE: solution.py\n```python\ndef mul(a, b):\n    return a * b\n```\n")
    tasks = [
        Task("add", "implement add", 'python3 -c "import solution as s; assert s.add(2,3)==5"'),
        Task("mul", "implement mul", 'python3 -c "import solution as s; assert s.mul(2,3)==6"'),
    ]
    rep = asyncio.run(run_benchmark([A, B], tasks=tasks, console=quiet_console(), rounds=2))
    check("bench per-model matrix is correct (A solves add only)",
          rep.outcomes[0].per_model == {"Groq": True, "OpenRouter": False})
    check("bench per-model matrix is correct (B solves mul only)",
          rep.outcomes[1].per_model == {"Groq": False, "OpenRouter": True})
    check("bench best single model = 50%", abs(rep.best_single() - 50.0) < 1e-6, f"{rep.best_single()}")
    check("bench three-head select = 100% (union beats best individual)",
          abs(rep.select_rate() - 100.0) < 1e-6, f"{rep.select_rate()}")
    check("bench revise rate = 100% (both tasks reach VERIFIED)", abs(rep.revise_rate() - 100.0) < 1e-6)
    check("format_report renders without error", format_report(rep).row_count >= 2)
    check("headline mentions the comparison", "three-head" in headline(rep) and "%" in headline(rep))

    # A head that solves nothing must drag its own rate to 0 but not crash aggregation.
    Z = MockAgent("nim", "NIM", "no edits here, just talk")
    rep2 = asyncio.run(run_benchmark([A, Z], tasks=tasks[:1], console=quiet_console(), rounds=1))
    check("bench handles a non-editing head", rep2.model_rate("NIM") == 0.0 and rep2.select_rate() == 100.0)

    # multi-seed aggregation: mean ± stdev across runs (the variance that makes it defensible).
    from triad.bench import aggregate_runs, multi_headline, BenchReport, TaskOutcome
    r_hi = BenchReport(["Groq"], [TaskOutcome("t", {"Groq": True}, True, True)], 1)   # select 100%
    r_lo = BenchReport(["Groq"], [TaskOutcome("t", {"Groq": False}, False, False)], 1)  # select 0%
    agg = aggregate_runs([r_hi, r_lo])
    check("multi-seed mean is correct", abs(agg["select"]["mean"] - 50.0) < 1e-6, str(agg["select"]))
    check("multi-seed reports stdev (variance)", abs(agg["select"]["sd"] - 50.0) < 1e-6)
    check("multi-seed reports min/max range", agg["select"]["min"] == 0.0 and agg["select"]["max"] == 100.0)
    check("multi-seed: identical runs give zero variance",
          aggregate_runs([r_hi, r_hi])["select"]["sd"] == 0.0)
    check("multi-headline shows ± spread", "±" in multi_headline(agg) and "seeds" in multi_headline(agg))


def test_memory():
    print("\n[memory: vault-backed lexical recall — token-saving]")
    from triad.memory import VaultMemory, _tokens

    check("tokenizer drops stopwords + 1-char tokens", _tokens("The a I of sandbox") == ["sandbox"])

    with tempfile.TemporaryDirectory() as d:
        vault = Path(d)
        (vault / "topics").mkdir()
        (vault / "topics" / "execution sandbox.md").write_text(
            "# execution sandbox\n\nRun untrusted model-generated code with no network and a "
            "filesystem boundary. Never on the host. Docker or seatbelt tiers.\n")
        (vault / "topics" / "oracle independence.md").write_text(
            "# oracle independence\n\nThe verifier must not be authored by the candidate it grades. "
            "Use the user's tests or ground-truth execution.\n")
        (vault / "topics" / "cooking.md").write_text(
            "# cooking\n\nTo make pasta, boil water, add salt, cook for nine minutes, then drain.\n")

        mem = VaultMemory(vault)
        check("memory indexes the notes", mem.ready and mem.stats()["notes"] == 3)

        hits = mem.rank("how do I safely run untrusted code in a sandbox", k=3)
        check("recall ranks the relevant note first",
              hits and hits[0][1] == "execution sandbox", hits[0][1] if hits else "none")
        names = [h[1] for h in hits]
        check("recall excludes the irrelevant note", "cooking" not in names)

        block = mem.recall("untrusted code network filesystem isolation")
        check("recall block tags its source as a wikilink", "[[execution sandbox]]" in block)
        check("recall block omits unrelated content", "pasta" not in block)
        check("empty/irrelevant query recalls nothing", mem.recall("zzz qqq xyzzy") == "")

        sv = mem.savings("untrusted code sandbox isolation")
        check("recall costs fewer tokens than the whole vault", sv["recall_tokens"] < sv["full_tokens"])
        check("savings reported as a positive percentage", sv["saved_pct"] > 0, str(sv))


def test_memory_wiring():
    print("\n[memory wiring: recall injected + history bounded in parallel]")

    class FakeMem:
        def __init__(self, block): self.block, self.queries = block, []
        def recall(self, q, *a, **k): self.queries.append(q); return self.block

    # recall block is injected into the system prompt for the turn, but NOT stored in history.
    agents = [MockAgent("groq", "Groq", "ok"), MockAgent("openrouter", "OpenRouter", "ok")]
    orch = Orchestrator(agents, {}, quiet_console(), mode="parallel")
    orch.memory = FakeMem("## Relevant memory\n- [[execution sandbox]]: never on the host")
    asyncio.run(orch.dispatch("how do we run code safely"))
    sys_seen = agents[0].calls[-1][1]
    check("recall block injected into the system prompt", "execution sandbox" in (sys_seen or ""))
    check("recalled context is NOT stored in history (no compounding)",
          all("Relevant memory" not in m["content"] for m in agents[0].history))
    check("memory was queried with the task", orch.memory.queries == ["how do we run code safely"])

    # history is capped so re-sent context stops growing; older turns live in the vault.
    a = MockAgent("groq", "Groq", "ok")
    orch2 = Orchestrator([a], {}, quiet_console(), mode="parallel")
    orch2.history_limit = 2
    for i in range(5):
        asyncio.run(orch2.dispatch(f"turn {i}"))
    check("history bounded to last N exchanges", len(a.history) <= 4, f"len={len(a.history)}")
    check("bounded history keeps the most recent turn", a.history[-2]["content"] == "turn 4")


def test_evict_and_tokens():
    print("\n[summarize-on-evict + token-savings benchmark]")
    from triad import vault as V
    from triad.bench import history_growth_savings, vault_recall_savings, token_headline
    from triad.memory import VaultMemory

    # ---- summarize-on-evict: trimmed turns are handed to the sink, and archived to the vault ----
    captured = []
    a = MockAgent("groq", "Groq", "ok")
    orch = Orchestrator([a], {}, quiet_console(), mode="parallel")
    orch.history_limit = 1
    orch.on_evict = lambda ag, dropped: captured.append((ag.name, list(dropped)))
    for i in range(3):
        asyncio.run(orch.dispatch(f"question {i}"))
    check("on_evict fires when history overflows", len(captured) >= 1)
    check("evicted payload carries the dropped exchange",
          any(m["content"] == "question 0" for _, msgs in captured for m in msgs))

    with tempfile.TemporaryDirectory() as d:
        msgs = [{"role": "user", "content": "how does the sandbox block network"},
                {"role": "assistant", "content": "It uses a seatbelt no-net floor verified at startup."}]
        n = V.archive_evicted(d, "groq", "Groq", msgs)
        check("archive_evicted writes one exchange", n == 1)
        note = (Path(d) / "memory" / "groq.md").read_text()
        check("archived note is recallable (Q + A persisted)", "sandbox" in note and "seatbelt" in note)
        mem = VaultMemory(d)
        check("recall finds the archived (evicted) turn",
              "seatbelt" in mem.recall("network isolation sandbox"))

    # ---- token-savings benchmark ----
    growth = history_growth_savings(turns=20, turn_tokens=300, limit=6, recall_tokens=400)
    check("history-growth: bounded beats unbounded re-send", growth["bounded"] < growth["baseline"])
    check("history-growth: positive saving even at 20 turns", growth["saved_pct"] > 0, str(growth))
    # the defensible claim: savings GROW with conversation length (quadratic re-send vs linear).
    longer = history_growth_savings(turns=60, turn_tokens=300, limit=6, recall_tokens=400)
    check("history-growth: savings grow with chat length",
          longer["saved_pct"] > growth["saved_pct"], f"{growth['saved_pct']}% -> {longer['saved_pct']}%")

    with tempfile.TemporaryDirectory() as d:
        vd = Path(d)
        (vd / "topics").mkdir()
        (vd / "topics" / "execution sandbox.md").write_text(
            "# execution sandbox\n\n" + ("Run untrusted model code with no network, filesystem bounded. " * 40))
        (vd / "topics" / "decorrelation.md").write_text(
            "# decorrelation\n\n" + ("Free models with distinct lineages make independent errors. " * 40))
        mem = VaultMemory(vd)
        rows = vault_recall_savings(mem, ["how is untrusted code sandboxed and network blocked"])
        check("recall costs far fewer tokens than the whole vault", rows[0][2] < rows[0][1])
        check("token headline reports a percentage", "%" in token_headline(rows, growth))


def main():
    test_protocol()
    test_dotenv()
    test_skills()
    test_mask()
    test_build_agents_no_keys()
    test_parallel()
    test_relay()
    test_relay_protocol()
    test_council()
    test_one_agent_error_isolated()
    test_activate_new_no_dup()
    test_save_all_modes()
    test_parallel_cleanup_on_cancel()
    test_free_roster()
    test_vault()
    test_sandbox()
    test_sandbox_node()
    test_verify()
    test_coder()
    test_bench()
    test_memory()
    test_memory_wiring()
    test_evict_and_tokens()
    print(f"\n{'='*50}\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("FAILED:", ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
