"""Local browser UI for triad — same agents, in a chat window instead of the REPL.

`python -m triad serve` (or `--web`) starts a small HTTP server on localhost and opens
your browser. You pick a mode (parallel / relay / council) and talk to the roster; replies
stream in live, one card per agent.

Design notes (why it's built this way):
  * Stdlib only. No FastAPI/uvicorn — fits the no-budget ethos and means zero install.
    http.server (threaded) + Server-Sent Events (SSE) for streaming.
  * ONE persistent asyncio loop runs in a background thread for the whole server. Provider
    SDK clients (AsyncOpenAI / AsyncAnthropic) bind to the loop they're first used on, so —
    exactly like the REPL's single-loop design — every request must run on that same loop.
    Each new asyncio.run() would create a fresh loop and break the clients on the 2nd turn.
  * The HTTP handler runs in its own thread; it submits the streaming coroutine to the shared
    loop with run_coroutine_threadsafe and bridges deltas back over a thread-safe queue.
  * Verify mode is intentionally left to the terminal: it's an execution pipeline (oracle +
    sandbox), not a chat. parallel/relay/council mirror the orchestrator's non-protocol prompts.
"""
from __future__ import annotations

import asyncio
import json
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List

from . import config, dotenv, keychain
from .agents import Agent, build_agents
from .setup_keys import CORE, EXTRA

DEFAULT_ENV = Path(__file__).resolve().parent.parent / ".env"

# rich color names (agents.py) -> CSS hex, so the browser cards match the terminal palette.
COLORS = {
    "green": "#4ade80", "magenta": "#e879f9", "bright_cyan": "#22d3ee",
    "bright_green": "#86efac", "dark_orange3": "#fb923c", "blue": "#60a5fa",
    "white": "#e5e7eb",
}

ASSETS = Path(__file__).resolve().parent / "assets"


def _dragon_art() -> str:
    """The three-headed-dragon ASCII portrait shown in the web UI's left rail."""
    try:
        art = (ASSETS / "dragon.txt").read_text(encoding="utf-8")
    except OSError:
        return ""
    return art.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class Session:
    """Server-wide state: the live agent list + the shared event loop. One user, local."""

    def __init__(self, roster: str) -> None:
        self.roster = roster
        self.agents: List[Agent] = build_agents(roster)
        self.lock = threading.Lock()          # one task at a time (agents/history are shared)
        self._bubble = 0

        # Background thread owning the asyncio loop every request runs on.
        self.loop = asyncio.new_event_loop()
        t = threading.Thread(target=self._run_loop, daemon=True)
        t.start()

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def next_bubble(self) -> int:
        self._bubble += 1
        return self._bubble

    def reset(self) -> None:
        for a in self.agents:
            a.clear()

    # ----------------------------------------------------------- orchestration
    async def _stream_bubble(self, a: Agent, q: "queue.Queue", *, task=None,
                             messages=None, label=None) -> str:
        """Stream one agent into one browser card. task -> persistent history (parallel);
        messages -> ephemeral (relay/council), matching the orchestrator."""
        bid = self.next_bubble()
        q.put({"type": "start", "id": bid, "agent": a.name,
               "label": label or a.label, "color": COLORS.get(a.color, "#e5e7eb"),
               "model": a.model})
        text = ""
        try:
            gen = a.stream(task) if task is not None else a.stream_raw(messages)
            async for delta in gen:
                text += delta
                q.put({"type": "delta", "id": bid, "text": delta})
        except Exception as e:  # surface provider errors in the card, don't kill the turn
            q.put({"type": "delta", "id": bid, "text": f"\n\n⚠ {type(e).__name__}: {e}"})
        q.put({"type": "end", "id": bid})
        return text

    async def run_parallel(self, task: str, q: "queue.Queue") -> None:
        await asyncio.gather(*[self._stream_bubble(a, q, task=task) for a in self.agents])

    async def run_relay(self, task: str, q: "queue.Queue") -> None:
        raw_accum = ""
        for i, a in enumerate(self.agents):
            if i == 0:
                prompt = task
            else:
                prompt = (
                    "You are collaborating with other AI agents on a shared task.\n\n"
                    f"TASK:\n{task}\n\nWORK SO FAR (from other agents):\n{raw_accum.strip()}\n\n"
                    "Add your contribution: build on it, fix mistakes, fill gaps. "
                    "Don't just repeat what's already there."
                )
            q.put({"type": "rule", "text": f"Relay step {i + 1}/{len(self.agents)} — {a.label}"})
            text = await self._stream_bubble(
                a, q, messages=[{"role": "user", "content": prompt}],
                label=f"{a.label} · step {i + 1}")
            raw_accum += f"\n\n## {a.label}\n{text}"

    async def run_council(self, task: str, q: "queue.Queue") -> None:
        q.put({"type": "rule", "text": "Round 1 — independent answers"})
        answers: Dict[str, str] = {}

        async def one(a: Agent) -> None:
            answers[a.name] = await self._stream_bubble(
                a, q, messages=[{"role": "user", "content": task}])

        await asyncio.gather(*[one(a) for a in self.agents])

        body = "\n\n".join(
            f"### Response {chr(65 + i)}\n{answers[a.name]}" for i, a in enumerate(self.agents))
        chair = self.agents[0]
        q.put({"type": "rule", "text": f"Round 2 — {chair.label} synthesizes"})
        synth = (
            "Several AI agents independently answered the task below. As the chair, "
            "produce the single best final answer: merge their strengths, resolve "
            "disagreements, correct errors, and flag any important dissent.\n\n"
            f"TASK:\n{task}\n\nRESPONSES:\n{body}"
        )
        await self._stream_bubble(
            chair, q, messages=[{"role": "user", "content": synth}],
            label=f"{chair.label} · synthesis")

    def dispatch(self, mode: str, task: str, q: "queue.Queue") -> None:
        """Run a turn on the shared loop, draining events to `q`; sentinel None when done."""
        runner = {"relay": self.run_relay, "council": self.run_council}.get(mode, self.run_parallel)
        fut = asyncio.run_coroutine_threadsafe(runner(task, q), self.loop)
        try:
            fut.result()  # propagate unexpected errors after the stream drains
        except Exception as e:
            q.put({"type": "rule", "text": f"error: {type(e).__name__}: {e}"})
        finally:
            q.put(None)


# --------------------------------------------------------------------------- HTTP
def _handler(session: Session):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                page = PAGE.replace("__DRAGON__", _dragon_art())
                self._send(200, page.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/agents":
                data = {
                    "roster": session.roster,
                    "agents": [{"name": a.name, "label": a.label,
                                "color": COLORS.get(a.color, "#e5e7eb"), "model": a.model}
                               for a in session.agents],
                }
                self._send(200, json.dumps(data).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            if self.path == "/api/reset":
                session.reset()
                self._send(200, b'{"ok":true}', "application/json")
                return
            if self.path != "/api/send":
                self._send(404, b"not found", "text/plain")
                return

            mode = payload.get("mode", "parallel")
            task = (payload.get("task") or "").strip()
            if not task:
                self._send(400, b'{"error":"empty task"}', "application/json")
                return
            if not session.lock.acquire(blocking=False):
                self._send(409, b'{"error":"busy"}', "application/json")
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()

            q: "queue.Queue" = queue.Queue()
            worker = threading.Thread(
                target=session.dispatch, args=(mode, task, q), daemon=True)
            worker.start()
            try:
                while True:
                    evt = q.get()
                    if evt is None:
                        self.wfile.write(b"data: " + json.dumps({"type": "done"}).encode() + b"\n\n")
                        self.wfile.flush()
                        break
                    self.wfile.write(b"data: " + json.dumps(evt).encode() + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser navigated away mid-stream
            finally:
                session.lock.release()

    return Handler


def serve(roster: str = "paid", host: str = "127.0.0.1", port: int = 8770,
          open_browser: bool = True) -> None:
    # Make keys available exactly like the REPL does (so no `source` needed).
    providers = {"paid": CORE, "free": EXTRA, "all": CORE + EXTRA}[roster]
    dotenv.load(DEFAULT_ENV)
    keychain.load_missing([p.env for p in providers])

    session = Session(roster)
    if not session.agents:
        print(f"No API keys found for the '{roster}' roster. "
              f"Run: python -m triad setup{' --all' if roster != 'paid' else ''}")
        return

    httpd = ThreadingHTTPServer((host, port), _handler(session))
    url = f"http://{host}:{port}"
    names = ", ".join(a.label for a in session.agents)
    print(f"triad web · roster={roster} · agents: {names}")
    print(f"serving on {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye 👋")
        httpd.shutdown()


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>triad</title>
<style>
  :root { --bg:#0b0d12; --panel:#12151c; --panel2:#1a1f29; --line:#262c38;
          --text:#e7eaf0; --dim:#8b93a4; --accent:#fb923c; }
  * { box-sizing:border-box; }
  html,body { margin:0; height:100%; }
  body { background:var(--bg); color:var(--text); font:15px/1.55 -apple-system,BlinkMacSystemFont,
         "Segoe UI",Roboto,Helvetica,Arial,sans-serif; display:flex; flex-direction:column; }
  .shell { flex:1; display:flex; min-height:0; }
  .content { flex:1; display:flex; flex-direction:column; min-width:0; }
  .rail { width:344px; flex-shrink:0; border-right:1px solid var(--line);
          background:radial-gradient(120% 60% at 50% 12%, #15110c 0%, #0a0c11 60%);
          display:flex; flex-direction:column; align-items:center; justify-content:center;
          gap:18px; padding:22px 12px; overflow:hidden; }
  .dragon { margin:0; white-space:pre; font-family:ui-monospace,Menlo,Consolas,monospace;
            font-size:4.6px; line-height:4.8px; letter-spacing:0;
            background:linear-gradient(180deg,#fde68a 0%,#fb923c 38%,#b91c1c 82%,#5b1208 100%);
            -webkit-background-clip:text; background-clip:text; color:transparent;
            animation:ember 5s ease-in-out infinite; }
  @keyframes ember { 0%,100% { filter:drop-shadow(0 0 5px rgba(251,146,60,.16)); opacity:.9; }
                     50%     { filter:drop-shadow(0 0 12px rgba(251,146,60,.45)); opacity:1; } }
  .rail-cap { text-align:center; }
  .rail-cap b { display:block; color:var(--accent); font-size:18px; font-weight:700; letter-spacing:4px; }
  .rail-cap span { display:block; color:var(--dim); font-size:11px; margin-top:5px; letter-spacing:.5px; }
  @media (max-width:880px) { .rail { display:none; } }
  header { display:flex; align-items:center; gap:14px; padding:12px 18px;
           border-bottom:1px solid var(--line); background:var(--panel); }
  header h1 { font-size:16px; margin:0; letter-spacing:.5px; }
  header h1 b { color:var(--accent); }
  .meta { color:var(--dim); font-size:12.5px; }
  .modes { margin-left:auto; display:flex; gap:6px; }
  .modes button { background:var(--panel2); color:var(--dim); border:1px solid var(--line);
                  padding:6px 12px; border-radius:8px; cursor:pointer; font-size:13px; }
  .modes button.active { color:#fff; border-color:var(--accent); background:#241a12; }
  #reset { background:transparent; border:1px solid var(--line); color:var(--dim);
           padding:6px 10px; border-radius:8px; cursor:pointer; font-size:13px; }
  main { flex:1; overflow-y:auto; padding:20px; max-width:980px; width:100%; margin:0 auto; }
  .turn { margin-bottom:22px; }
  .usermsg { color:var(--text); background:var(--panel2); border:1px solid var(--line);
             padding:10px 14px; border-radius:10px; margin-bottom:12px; white-space:pre-wrap; }
  .usermsg::before { content:"you"; display:block; color:var(--dim); font-size:11px;
                     text-transform:uppercase; letter-spacing:.6px; margin-bottom:4px; }
  .rule { color:var(--dim); font-size:12px; text-transform:uppercase; letter-spacing:.8px;
          text-align:center; margin:14px 0 10px; position:relative; }
  .rule::before,.rule::after { content:""; position:absolute; top:50%; width:38%;
          height:1px; background:var(--line); } .rule::before{left:0;} .rule::after{right:0;}
  .cards { display:flex; flex-wrap:wrap; gap:12px; }
  .card { flex:1 1 260px; min-width:240px; background:var(--panel); border:1px solid var(--line);
          border-top:2px solid var(--c,#888); border-radius:10px; padding:10px 13px; }
  .card .who { font-size:12px; font-weight:600; color:var(--c,#fff); margin-bottom:6px;
               display:flex; justify-content:space-between; gap:8px; }
  .card .who small { color:var(--dim); font-weight:400; }
  .card .body { white-space:pre-wrap; word-break:break-word; font-size:14px; }
  .card .body code { background:#0008; padding:1px 4px; border-radius:4px; }
  .cursor::after { content:"▋"; color:var(--c); animation:b 1s steps(2) infinite; }
  @keyframes b { 50%{opacity:0;} }
  footer { border-top:1px solid var(--line); background:var(--panel); padding:12px 18px; }
  .composer { max-width:980px; margin:0 auto; display:flex; gap:10px; }
  textarea { flex:1; resize:none; background:var(--panel2); color:var(--text);
             border:1px solid var(--line); border-radius:10px; padding:10px 12px;
             font:inherit; min-height:46px; max-height:180px; }
  #send { background:var(--accent); color:#1a1208; border:0; border-radius:10px;
          padding:0 20px; font-weight:600; cursor:pointer; }
  #send:disabled { opacity:.5; cursor:default; }
  .hint { color:var(--dim); font-size:11.5px; text-align:center; margin-top:6px; }
</style></head>
<body>
<header>
  <h1><b>triad</b></h1>
  <span class="meta" id="meta">loading…</span>
  <div class="modes" id="modes">
    <button data-m="parallel" class="active">parallel</button>
    <button data-m="relay">relay</button>
    <button data-m="council">council</button>
  </div>
  <button id="reset">reset</button>
</header>
<div class="shell">
<aside class="rail">
  <pre class="dragon">__DRAGON__</pre>
  <div class="rail-cap"><b>TRIAD</b><span>three heads · one verdict</span></div>
</aside>
<section class="content">
<main id="main"></main>
<footer>
  <div class="composer">
    <textarea id="input" placeholder="Ask the roster…  (Enter to send · Shift+Enter for newline)"></textarea>
    <button id="send">send</button>
  </div>
  <div class="hint" id="hint">parallel: everyone answers at once · relay: in sequence · council: all answer, chair synthesizes</div>
</footer>
</section>
</div>
<script>
const main=document.getElementById('main'), input=document.getElementById('input'),
      send=document.getElementById('send'), meta=document.getElementById('meta');
let mode='parallel', busy=false;
const HINTS={parallel:"parallel: everyone answers at once, independently",
  relay:"relay: agents work in sequence, each building on the last",
  council:"council: all answer, then the first agent synthesizes the best single answer"};

document.querySelectorAll('#modes button').forEach(b=>b.onclick=()=>{
  if(busy) return;
  document.querySelectorAll('#modes button').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); mode=b.dataset.m;
  document.getElementById('hint').textContent=HINTS[mode];
});

fetch('/api/agents').then(r=>r.json()).then(d=>{
  meta.textContent=`roster: ${d.roster} · ${d.agents.map(a=>a.label).join(', ')}`;
});

document.getElementById('reset').onclick=async()=>{
  await fetch('/api/reset',{method:'POST',body:'{}'});
  main.innerHTML='';
};

input.addEventListener('keydown',e=>{
  if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); go(); }
});
send.onclick=go;

function esc(s){return s.replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}

async function go(){
  const task=input.value.trim();
  if(!task || busy) return;
  busy=true; send.disabled=true; input.value='';

  const turn=document.createElement('div'); turn.className='turn';
  const um=document.createElement('div'); um.className='usermsg'; um.textContent=task;
  turn.appendChild(um);
  const cards=document.createElement('div'); cards.className='cards'; turn.appendChild(cards);
  main.appendChild(turn); main.scrollTop=main.scrollHeight;

  const bubbles={}; // id -> body element
  const res=await fetch('/api/send',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode,task})});
  if(res.status===409){ done('busy — wait for the current turn'); return; }
  const reader=res.body.getReader(), dec=new TextDecoder(); let buf='';
  while(true){
    const {value,done:d}=await reader.read(); if(d) break;
    buf+=dec.decode(value,{stream:true});
    let i;
    while((i=buf.indexOf('\n\n'))>=0){
      const line=buf.slice(0,i); buf=buf.slice(i+2);
      if(!line.startsWith('data: ')) continue;
      handle(JSON.parse(line.slice(6)));
    }
  }
  function handle(ev){
    if(ev.type==='rule'){
      const r=document.createElement('div'); r.className='rule'; r.textContent=ev.text;
      turn.appendChild(r);
    } else if(ev.type==='start'){
      const card=document.createElement('div'); card.className='card';
      card.style.setProperty('--c',ev.color);
      card.innerHTML=`<div class="who"><span>${esc(ev.label)}</span><small>${esc(ev.model)}</small></div>`+
                     `<div class="body cursor"></div>`;
      // parallel (no rules) -> side-by-side in .cards; relay/council -> full-width, in order.
      if(turn.querySelector('.rule')) turn.appendChild(card); else cards.appendChild(card);
      bubbles[ev.id]=card.querySelector('.body');
    } else if(ev.type==='delta'){
      const b=bubbles[ev.id]; if(b){ b.textContent+=ev.text; main.scrollTop=main.scrollHeight; }
    } else if(ev.type==='end'){
      const b=bubbles[ev.id]; if(b) b.classList.remove('cursor');
    } else if(ev.type==='done'){
      done();
    }
  }
  function done(msg){
    busy=false; send.disabled=false; input.focus();
    if(msg){ const r=document.createElement('div'); r.className='rule'; r.textContent=msg; turn.appendChild(r);}
  }
}
input.focus();
</script>
</body></html>"""
