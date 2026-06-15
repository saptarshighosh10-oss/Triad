"""Cass server — triad powering the astronaut game's AI crewmate.

One command serves the game's static files AND a `/api/cass` endpoint that generates Cass's next
line from a FREE model wearing the `crewmate` skill. The game owns the `[STATE]` (lucid → slipping
→ obsessive → hostile → resolved); the model fills the line. Serving over http also fixes the
file:// blank-screen problem — the game's ES-module imports load fine over localhost.

Architecture mirrors web.py: one persistent asyncio loop in a background thread (so the provider
client stays bound to a single loop across requests), driven from the HTTP handler via futures.
If no free key is configured, /api/cass returns {"line": null} and the game falls back to its
hand-written script — so it always works.
"""
from __future__ import annotations

import asyncio
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import dotenv, keychain
from .agents import build_agents
from .setup_keys import CORE, EXTRA
from .skills import load_skills

DEFAULT_ENV = Path(__file__).resolve().parent.parent / ".env"
SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
_CTYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".mjs": "text/javascript",
           ".css": "text/css", ".json": "application/json", ".png": "image/png", ".jpg": "image/jpeg",
           ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class CassEngine:
    """A single free head wearing the crewmate skill, on a persistent event loop."""

    def __init__(self, roster: str = "free") -> None:
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self._run, daemon=True).start()
        self.agents = build_agents(roster)        # try each in turn so one 429 doesn't mute Cass
        sk = load_skills(str(SKILLS_DIR)).get("crewmate")
        self.system = sk.body if sk else ""
        for a in self.agents:
            if self.system:
                a.set_system(self.system)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    @property
    def ready(self) -> bool:
        return bool(self.agents)

    @property
    def agent(self):
        return self.agents[0] if self.agents else None

    def line(self, state: str, player: str, history: str) -> str | None:
        if not self.agents:
            return None
        prompt = f"[STATE: {state}]\n"
        if history:
            prompt += f"Earlier, you said:\n{history}\n\n"
        said = player.strip() or "(they just stand with you at the window, saying nothing)"
        prompt += (f'The crewmate beside you says: "{said}"\n\n'
                   f"Reply as Cass — one or two sentences, fully in the {state} register. "
                   "Only the spoken line, no narration, no quotation marks.")
        for a in self.agents:                     # first head that answers wins (rate-limit resilient)
            fut = asyncio.run_coroutine_threadsafe(
                a.complete_raw([{"role": "user", "content": prompt}], system=self.system), self.loop)
            try:
                out = fut.result(timeout=40).strip().strip('"').strip()
                if out:
                    return out
            except Exception:
                continue
        return None


def _handler(engine: CassEngine, game_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            rel = self.path.split("?", 1)[0].lstrip("/") or "index.html"
            fp = (game_dir / rel).resolve()
            if game_dir not in fp.parents and fp != game_dir / rel:  # crude traversal guard
                fp = game_dir / "index.html"
            if not fp.is_file():
                fp = game_dir / "index.html"
            if not fp.is_file():
                self._send(404, b"game file not found", "text/plain")
                return
            self._send(200, fp.read_bytes(), _CTYPES.get(fp.suffix, "application/octet-stream"))

        def do_POST(self):
            if self.path.split("?")[0] != "/api/cass":
                self._send(404, b"not found", "text/plain")
                return
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            line = engine.line(payload.get("state", "lucid"),
                               payload.get("player", ""), payload.get("history", ""))
            self._send(200, json.dumps({"line": line}).encode(), "application/json")

    return Handler


def serve(game_dir: str, roster: str = "free", host: str = "127.0.0.1", port: int = 8095,
          open_browser: bool = True) -> None:
    providers = {"paid": CORE, "free": EXTRA, "all": CORE + EXTRA}[roster]
    dotenv.load(DEFAULT_ENV)
    keychain.load_missing([p.env for p in providers])

    gd = Path(game_dir).resolve()
    if not (gd / "index.html").is_file():
        print(f"No index.html in {gd} — point --game at the game folder.")
        return

    engine = CassEngine(roster)
    httpd = ThreadingHTTPServer((host, port), _handler(engine, gd))
    url = f"http://{host}:{port}"
    who = engine.agent.label if engine.ready else "none (scripted fallback)"
    print(f"Cassiopeia + triad · game: {gd}")
    print(f"Cass voiced by: {who}   serving on {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye 👋")
        httpd.shutdown()
