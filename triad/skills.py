"""Skill files: markdown with a tiny YAML-ish frontmatter block.

    ---
    name: code-reviewer
    description: Hunts for bugs, edge cases, and security issues
    agents: [gemini]        # who it targets by default; "all" = everyone
    ---
    <instructions the agent receives as part of its system prompt>

The frontmatter parser is intentionally minimal so there's no PyYAML dependency.
"""
from __future__ import annotations

import glob
import os
from dataclasses import dataclass, field
from typing import Dict, List, Tuple


@dataclass
class Skill:
    name: str
    description: str = ""
    agents: List[str] = field(default_factory=lambda: ["all"])
    body: str = ""


def _parse_frontmatter(text: str) -> Tuple[Dict[str, str], str]:
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            block = text[3:end].strip()
            body = text[end + 4 :].lstrip("\n")
            for line in block.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
    return meta, body


def load_skills(path: str) -> Dict[str, Skill]:
    skills: Dict[str, Skill] = {}
    if not os.path.isdir(path):
        return skills
    for fp in sorted(glob.glob(os.path.join(path, "*.md"))):
        with open(fp, encoding="utf-8") as fh:
            meta, body = _parse_frontmatter(fh.read())
        name = meta.get("name") or os.path.splitext(os.path.basename(fp))[0]
        raw = meta.get("agents", "all").strip().strip("[]")
        agents = [a.strip() for a in raw.split(",") if a.strip()] or ["all"]
        skills[name] = Skill(
            name=name,
            description=meta.get("description", ""),
            agents=agents,
            body=body.strip(),
        )
    return skills
