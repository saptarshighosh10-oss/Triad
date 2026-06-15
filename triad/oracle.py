"""Independent verification for the `verify` mode (generate-verify-select).

An Oracle decides pass/fail for a candidate WITHOUT the candidate authoring its own pass
condition — that's *oracle independence*. When no independent oracle is available, evaluation
returns 'unverified' and the mode reports "selection only": it never fabricates a pass and never
lets a candidate grade itself.

Oracles here:
  CommandOracle  — run a user-authored command (e.g. `pytest -q`, or a `python3 -c "..."` check)
                   against the candidate's code in the sandbox. Independent: the command/tests are
                   the user's; the candidate only supplies the code under test.
  AbsentOracle   — no oracle configured -> every result is 'unverified' (selection only).

A separate test-author model (writes tests BEFORE seeing candidates) is a future independent
oracle; the interface below is ready for it. See the vault note "open questions — verify mode".
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from .sandbox import Sandbox

_FENCE = re.compile(r"```[a-zA-Z0-9_+.\-]*\n(.*?)```", re.DOTALL)


def extract_code(text: str) -> str:
    """The candidate's solution: the largest fenced code block, or the raw text if there's none."""
    blocks = _FENCE.findall(text or "")
    if blocks:
        return max(blocks, key=len).strip()
    return (text or "").strip()


@dataclass
class Verdict:
    status: str            # "pass" | "fail" | "unverified"
    detail: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "pass"

    @property
    def verified(self) -> bool:
        return self.status in ("pass", "fail")


class Oracle:
    name = "oracle"
    independent = True       # the pass condition is NOT authored by the candidate being graded

    def describe(self) -> str:
        return self.name

    def check(self, candidate_text: str, sandbox: Sandbox) -> Verdict:
        raise NotImplementedError

    def check_workspace(self, files: Dict[str, str], sandbox: Sandbox) -> Verdict:
        """Grade a whole edited file tree (relpath -> content), for multi-file edit jobs.

        Same independence guarantee as check(): the command/fixtures are the user's; the
        candidate only supplies the files under test. Default: not supported.
        """
        raise NotImplementedError


class AbsentOracle(Oracle):
    """No independent oracle — selection only. Never a self-grade, never a fabricated pass."""
    name = "absent"
    independent = False

    def describe(self) -> str:
        return "none (unverified — selection only)"

    def check(self, candidate_text: str, sandbox: Sandbox) -> Verdict:
        return Verdict("unverified", "no oracle configured")


class CommandOracle(Oracle):
    """Run a user-authored command against the candidate's extracted code, in the sandbox.

    Independence holds by construction: `command` and `fixtures` come from the user (--oracle /
    /oracle), never from the candidate — the candidate only supplies the code under test. Pass iff
    the command exits 0 (and doesn't time out).
    """
    name = "command"

    def __init__(self, command: Union[str, List[str]], solution_name: str = "solution.py",
                 fixtures: Optional[Dict[str, str]] = None, timeout: int = 20):
        self.command = command
        self.solution_name = solution_name
        self.fixtures = fixtures or {}
        self.timeout = timeout

    def describe(self) -> str:
        cmd = self.command if isinstance(self.command, str) else " ".join(self.command)
        return f"command: {cmd}"

    def check(self, candidate_text: str, sandbox: Sandbox) -> Verdict:
        code = extract_code(candidate_text)
        if not code:
            return Verdict("fail", "candidate produced no code")
        files = {self.solution_name: code, **self.fixtures}
        argv = ["/bin/sh", "-c", self.command] if isinstance(self.command, str) else list(self.command)
        return self._verdict_from_run(sandbox.run(files, argv, timeout=self.timeout))

    def check_workspace(self, files: Dict[str, str], sandbox: Sandbox) -> Verdict:
        """Run the command against a full edited file tree. Fixtures override repo files (e.g. to
        inject the test the candidate's code must pass). Pass iff the command exits 0."""
        if not files:
            return Verdict("fail", "candidate produced no files")
        argv = ["/bin/sh", "-c", self.command] if isinstance(self.command, str) else list(self.command)
        return self._verdict_from_run(sandbox.run({**files, **self.fixtures}, argv, timeout=self.timeout))

    @staticmethod
    def _verdict_from_run(r) -> Verdict:
        if r.timed_out:
            return Verdict("fail", f"timed out [{r.tier}]")
        msg = (r.stdout.strip() or r.stderr.strip())
        if len(msg) > 240:
            msg = msg[-240:]
        return Verdict("pass" if r.returncode == 0 else "fail", f"rc={r.returncode} [{r.tier}] {msg}".strip())
