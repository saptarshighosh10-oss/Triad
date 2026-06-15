"""Run untrusted, model-generated code with as much isolation as the host actually offers.

Stage 2 (generate-verify-select) executes code that *free models wrote*. This module never runs
it on the host unguarded, and it ALWAYS reports which isolation tier is active, so callers and
users can see exactly what is and isn't contained.

Tiers (auto-selected, strongest first):
  docker          real isolation — `--network none`, mem/cpu/pids caps, non-root, only a work dir
                  mounted. Network AND filesystem isolated.
  macos-seatbelt  macOS without Docker — `sandbox-exec` deny-network profile = a host-level no-net
                  floor, plus a CPU rlimit. NETWORK is blocked; FILESYSTEM is NOT isolated (the
                  code runs as you, with your file access).
  subprocess      fallback — temp cwd + scrubbed env + CPU rlimit only. NOT a security boundary:
                  no network block, no filesystem isolation. Reduced-isolation mode.

Honesty rules, baked in so nothing reads as safer than it is:
  * rlimits cap CPU/memory only — they do NOTHING for network or filesystem.
  * `Result.network_blocked` is True only when the tier truly blocks it (docker / seatbelt) — a
    "best-effort" attempt is never reported as a block.
  * untrusted code with network access can exfiltrate; the active tier and its gaps ride on every
    Result and on `Sandbox.note`, so the UI surfaces them instead of proceeding silently.
  * `sandbox-exec` is DEPRECATED by Apple (works today). We never assume it enforces the profile —
    at startup we run a real network attempt under it and claim the no-net floor only if that attempt
    is actually blocked. If it isn't (or sandbox-exec is gone), we degrade to subprocess and say so
    loudly: a broken seatbelt fails as "no isolation, warned", never "network silently open while we
    report it shut".
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

try:
    import resource  # POSIX only
except ImportError:  # pragma: no cover  (Windows)
    resource = None

# Allow normal operation but deny all network — keeps code runnable while removing the exfil path.
# (sandbox-exec is deprecated by Apple but still functional on current macOS.)
_SEATBELT_NO_NET = "(version 1)(allow default)(deny network*)"
_SAFE_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"

# Runtimes that may live outside the locked PATH (e.g. /usr/local/bin, nvm). We add ONLY the dir
# of each detected interpreter so JS/TS oracles (`node`) work, without opening the whole host PATH.
# Network is still blocked by the active tier, which is the real exfil control; this just lets the
# sandbox run JS/TS projects, not only Python. Note: docker-tier oracles need the tool in the image.
_EXTRA_RUNTIMES = ("node", "deno", "bun")
_extra_bin_cache = None


def _extra_bin_dirs() -> str:
    """':'-joined dirs of detected non-PATH runtimes (node/deno/bun), memoized; '' if none."""
    global _extra_bin_cache
    if _extra_bin_cache is None:
        dirs: List[str] = []
        for tool in _EXTRA_RUNTIMES:
            p = shutil.which(tool)
            if p:
                d = str(Path(p).parent)
                if d not in dirs and d not in _SAFE_PATH.split(":"):
                    dirs.append(d)
        _extra_bin_cache = ":".join(dirs)
    return _extra_bin_cache

# Run under the seatbelt profile to confirm it actually blocks the network. A real block raises
# PermissionError; anything else (connected, or some other error) means we can't claim a block.
_SEATBELT_PROBE = (
    "import socket\n"
    "try:\n"
    "    socket.create_connection(('1.1.1.1', 53), timeout=3); print('OPEN')\n"
    "except PermissionError: print('BLOCKED')\n"
    "except Exception as e: print('ERR:' + type(e).__name__)\n"
)
_detect_cache = None  # (tier, degrade_note) memoized per process — host capability is stable


def _seatbelt_blocks_network() -> bool:
    """Actually exercise the seatbelt no-net profile; True only if a connection is really blocked."""
    try:
        p = subprocess.run(
            ["sandbox-exec", "-p", _SEATBELT_NO_NET, sys.executable, "-c", _SEATBELT_PROBE],
            capture_output=True, text=True, timeout=15)
        return p.returncode == 0 and p.stdout.strip() == "BLOCKED"
    except Exception:
        return False


def _detect_tier():
    """Strongest tier the host *actually* provides. Seatbelt is verified, never assumed."""
    global _detect_cache
    if _detect_cache is None:
        if _docker_available():
            _detect_cache = ("docker", "")
        elif sys.platform == "darwin" and shutil.which("sandbox-exec"):
            if _seatbelt_blocks_network():
                _detect_cache = ("macos-seatbelt", "")
            else:
                _detect_cache = ("subprocess",
                    " sandbox-exec is present but its deny-network profile did NOT block a test "
                    "connection (the deprecated mechanism may have changed) — degraded to subprocess; "
                    "network is NOT blocked.")
        else:
            _detect_cache = ("subprocess", "")
    return _detect_cache


@dataclass
class Result:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    tier: str
    network_blocked: bool
    fs_isolated: bool
    note: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def fully_isolated(self) -> bool:
        """True only when untrusted code is contained on BOTH axes (network and filesystem)."""
        return self.network_blocked and self.fs_isolated


def _docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


class Sandbox:
    """Pick the strongest isolation tier the host supports and run code under it."""

    def __init__(self, prefer: Optional[str] = None, image: str = "python:3.13-slim") -> None:
        self.image = image
        # prefer is an explicit override (mostly for tests); otherwise use the *verified* tier.
        self.tier, self._degrade = (prefer, "") if prefer else _detect_tier()
        self.network_blocked = self.tier in ("docker", "macos-seatbelt")
        self.fs_isolated = self.tier == "docker"
        self.note = self._describe()

    @property
    def fully_isolated(self) -> bool:
        """True only when untrusted code is contained on BOTH axes (network and filesystem)."""
        return self.network_blocked and self.fs_isolated

    def _describe(self) -> str:
        if self.tier == "docker":
            return "docker — network + filesystem isolated, mem/cpu/pids capped, non-root."
        if self.tier == "macos-seatbelt":
            return ("macos-seatbelt — NETWORK blocked (no-net floor, verified at startup) + CPU cap. "
                    "FILESYSTEM NOT isolated: code runs as you. Install Docker for filesystem isolation.")
        return ("subprocess — REDUCED ISOLATION: temp cwd + scrubbed env + CPU cap only. "
                "NO network block, NO filesystem isolation; NOT a security boundary. Untrusted "
                "code can reach the network and your files. Install Docker for real isolation." + self._degrade)

    # ------------------------------------------------------------------ run
    def run(self, files: Dict[str, str], argv: List[str],
            timeout: int = 10, mem_mb: int = 512) -> Result:
        """Write `files` into a throwaway dir and run `argv` there under the active tier."""
        with tempfile.TemporaryDirectory(prefix="triad-sbx-") as tmp:
            root = Path(tmp)
            for name, content in files.items():
                fp = root / name
                fp.parent.mkdir(parents=True, exist_ok=True)
                fp.write_text(content, encoding="utf-8")
            if self.tier == "docker":
                cmd = self._docker_cmd(root, argv, mem_mb)
            elif self.tier == "macos-seatbelt":
                cmd = ["sandbox-exec", "-p", _SEATBELT_NO_NET, *argv]
            else:
                cmd = list(argv)
            return self._exec(cmd, root, timeout, mem_mb)

    def _docker_cmd(self, root: Path, argv: List[str], mem_mb: int) -> List[str]:
        return [
            "docker", "run", "--rm", "--network", "none",
            "--memory", f"{mem_mb}m", "--memory-swap", f"{mem_mb}m",
            "--cpus", "1", "--pids-limit", "128",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{root}:/work", "-w", "/work",
            self.image, *argv,
        ]

    def _exec(self, cmd: List[str], cwd: Path, timeout: int, mem_mb: int) -> Result:
        # Scrubbed env so inherited secrets (API keys, etc.) never reach untrusted code; HOME -> tmp.
        # PATH = locked system dirs + only the dirs of detected runtimes (node/deno/bun), so JS/TS
        # oracles run without exposing the whole host PATH. Network stays blocked by the tier.
        extra = _extra_bin_dirs()
        path = f"{_SAFE_PATH}:{extra}" if extra else _SAFE_PATH
        env = {"PATH": path, "HOME": str(cwd), "TMPDIR": str(cwd), "LANG": "C.UTF-8"}

        def _limits():  # POSIX rlimits, set in the child before exec; docker enforces its own caps
            if resource is None:
                return
            for res, val in ((resource.RLIMIT_CPU, (max(1, timeout), max(1, timeout) + 1)),
                             (resource.RLIMIT_AS, (mem_mb * 1024 * 1024,) * 2)):  # macOS often ignores AS
                try:
                    resource.setrlimit(res, val)
                except Exception:
                    pass

        preexec = None if (self.tier == "docker" or resource is None) else _limits
        try:
            p = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True, text=True,
                               timeout=timeout + 2, preexec_fn=preexec)
            return self._result(p.returncode, p.stdout, p.stderr, False)
        except subprocess.TimeoutExpired as e:
            out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
            err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")
            return self._result(124, out or "", (err or "") + "\n[sandbox] timed out", True)
        except FileNotFoundError as e:
            return self._result(127, "", f"[sandbox] cannot exec {cmd[0]!r}: {e}", False)

    def _result(self, rc: int, out: str, err: str, timed_out: bool) -> Result:
        return Result(rc, out, err, timed_out, self.tier,
                      self.network_blocked, self.fs_isolated, self.note)


def default_sandbox() -> Sandbox:
    return Sandbox()
