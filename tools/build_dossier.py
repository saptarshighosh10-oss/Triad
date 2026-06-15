#!/usr/bin/env python3
"""Build a single PDF dossier of the whole triad project: plan, architecture,
usage, the inter-agent protocol, the free-routing config, and full source —
so it's one uploadable file instead of a pile of downloads.
"""
import datetime
import html
import re
import textwrap
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (Flowable, HRFlowable, KeepTogether, PageBreak,
                                Paragraph, Preformatted, SimpleDocTemplate,
                                Spacer, Table, TableStyle)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "triad_dossier.pdf"

ACCENT = colors.HexColor("#2563eb")
ACCENT2 = colors.HexColor("#0f766e")
CODEBG = colors.HexColor("#f4f4f5")
CODEBORDER = colors.HexColor("#d4d4d8")
MUTED = colors.HexColor("#52525b")

styles = getSampleStyleSheet()
S = {
    "h1": ParagraphStyle("h1", parent=styles["Heading1"], fontSize=18, spaceBefore=6,
                          spaceAfter=10, textColor=ACCENT),
    "h2": ParagraphStyle("h2", parent=styles["Heading2"], fontSize=13.5, spaceBefore=12,
                          spaceAfter=5, textColor=ACCENT2),
    "h3": ParagraphStyle("h3", parent=styles["Heading3"], fontSize=11, spaceBefore=9,
                          spaceAfter=3, textColor=colors.HexColor("#18181b")),
    "h4": ParagraphStyle("h4", parent=styles["Heading4"], fontSize=9.5, spaceBefore=7,
                          spaceAfter=2, textColor=MUTED),
    "body": ParagraphStyle("body", parent=styles["BodyText"], fontSize=9.5, leading=14,
                           spaceAfter=6, alignment=TA_LEFT),
    "bullet": ParagraphStyle("bullet", parent=styles["BodyText"], fontSize=9.5, leading=14,
                             leftIndent=14, bulletIndent=4, spaceAfter=2),
    "quote": ParagraphStyle("quote", parent=styles["BodyText"], fontSize=9.5, leading=14,
                            leftIndent=12, textColor=MUTED, borderColor=ACCENT,
                            borderWidth=0, spaceAfter=6, fontName="Helvetica-Oblique"),
    "code": ParagraphStyle("code", fontName="Courier", fontSize=7.2, leading=9.2,
                           textColor=colors.HexColor("#18181b")),
    "cover_title": ParagraphStyle("ct", parent=styles["Title"], fontSize=30, leading=34,
                                  textColor=ACCENT, spaceAfter=4),
    "cover_sub": ParagraphStyle("cs", parent=styles["Normal"], fontSize=12, leading=17,
                                textColor=MUTED),
    "toc": ParagraphStyle("toc", parent=styles["Normal"], fontSize=10.5, leading=18),
}


def inline(text: str) -> str:
    """Escape, then re-apply a safe subset of markdown inline formatting."""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"`([^`]+?)`", r'<font face="Courier" size="8.5">\1</font>', text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<font color="#2563eb">\1</font>', text)
    return text


S["codebox"] = ParagraphStyle("codebox", fontName="Courier", fontSize=7.2, leading=9.2,
                              textColor=colors.HexColor("#18181b"), backColor=CODEBG,
                              borderColor=CODEBORDER, borderWidth=0.5, borderPadding=6,
                              spaceBefore=2, spaceAfter=6)


def code_flowable(code: str, width=None):
    """A shaded, bordered code block that SPLITS across pages (long files are fine)."""
    # Courier lacks box-drawing/symbol glyphs -> they render as black boxes. Map to ASCII.
    repl = {"─": "-", "│": "|", "┌": "+", "┐": "+", "└": "+", "┘": "+", "├": "+",
            "┤": "+", "•": "*", "…": "...", "—": "-", "–": "-", "’": "'", "‘": "'",
            "“": '"', "”": '"', "✓": "[ok]", "✗": "[x]", "●": "*", "→": "->", "›": ">"}
    for k, v in repl.items():
        code = code.replace(k, v)
    wrapped = []
    for ln in code.split("\n"):
        wrapped.extend(textwrap.wrap(ln, width=100, subsequent_indent="    ",
                                     replace_whitespace=False, drop_whitespace=False) or [""])
    return Preformatted("\n".join(wrapped), S["codebox"])


def md_to_flowables(md: str, width: float):
    out = []
    lines = md.split("\n")
    i = 0
    para_buf = []

    def flush_para():
        if para_buf:
            out.append(Paragraph(inline(" ".join(para_buf)), S["body"]))
            para_buf.clear()

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # fenced code
        if stripped.startswith("```"):
            flush_para()
            i += 1
            buf = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append(Spacer(1, 2))
            out.append(code_flowable("\n".join(buf), width))
            out.append(Spacer(1, 4))
            continue

        # table (pipe rows with a separator line)
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|?[\s:|-]+\|", lines[i + 1]):
            flush_para()
            rows = []
            while i < len(lines) and "|" in lines[i]:
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                rows.append(cells)
                i += 1
            header, body_rows = rows[0], [r for r in rows[2:]]
            data = [[Paragraph(inline(c), S["body"]) for c in header]] + \
                   [[Paragraph(inline(c), S["body"]) for c in r] for r in body_rows]
            t = Table(data, hAlign="LEFT", colWidths=[width / len(header)] * len(header))
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                ("GRID", (0, 0), (-1, -1), 0.4, CODEBORDER),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, CODEBG]),
                ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ]))
            out.append(t)
            out.append(Spacer(1, 6))
            continue

        if not stripped:
            flush_para()
        elif stripped.startswith("#### "):
            flush_para(); out.append(Paragraph(inline(stripped[5:]), S["h4"]))
        elif stripped.startswith("### "):
            flush_para(); out.append(Paragraph(inline(stripped[4:]), S["h3"]))
        elif stripped.startswith("## "):
            flush_para(); out.append(Paragraph(inline(stripped[3:]), S["h2"]))
        elif stripped.startswith("# "):
            flush_para(); out.append(Paragraph(inline(stripped[2:]), S["h1"]))
        elif stripped in ("---", "***", "___"):
            flush_para(); out.append(HRFlowable(width="100%", thickness=0.5, color=CODEBORDER,
                                                spaceBefore=4, spaceAfter=8))
        elif stripped.startswith("> "):
            flush_para(); out.append(Paragraph(inline(stripped[2:]), S["quote"]))
        elif re.match(r"^[-*] ", stripped) or re.match(r"^\d+\. ", stripped):
            flush_para()
            txt = re.sub(r"^([-*]|\d+\.) ", "", stripped)
            out.append(Paragraph(inline(txt), S["bullet"], bulletText="•"))
        else:
            para_buf.append(stripped)
        i += 1
    flush_para()
    return out


def section_from_file(path: Path, width: float):
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines and lines[0].startswith("# "):   # drop the doc's own top title; we add a part header
        lines = lines[1:]
    return md_to_flowables("\n".join(lines), width)


def source_block(rel: str, width: float):
    code = (ROOT / rel).read_text(encoding="utf-8")
    return [Paragraph(inline(rel), S["h3"]), code_flowable(code, width), Spacer(1, 6)]


def footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7.5)
    canvas.setFillColor(MUTED)
    canvas.drawString(2 * cm, 1.1 * cm, "TriAgent — Project Dossier")
    canvas.drawRightString(A4[0] - 2 * cm, 1.1 * cm, f"{doc.page}")
    canvas.restoreState()


def build():
    doc = SimpleDocTemplate(str(OUT), pagesize=A4, topMargin=1.8 * cm, bottomMargin=1.8 * cm,
                            leftMargin=2 * cm, rightMargin=2 * cm,
                            title="TriAgent — Project Dossier", author="triad")
    W = doc.width
    story = []

    # cover
    story.append(Spacer(1, 5 * cm))
    story.append(Paragraph("TriAgent", S["cover_title"]))
    story.append(Paragraph("Project Dossier", ParagraphStyle(
        "x", parent=S["cover_sub"], fontSize=16, textColor=ACCENT2)))
    story.append(Spacer(1, 0.6 * cm))
    story.append(Paragraph(
        "A terminal orchestrator for ChatGPT + Claude + Gemini — parallel, relay, and "
        "council modes, a compact inter-agent protocol, an API-key wizard, and a plan "
        "for routing cheap work through free models.", S["cover_sub"]))
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(datetime.date.today().strftime("Generated %B %d, %Y"),
                           ParagraphStyle("d", parent=S["cover_sub"], fontSize=9)))
    story.append(PageBreak())

    # contents
    story.append(Paragraph("Contents", S["h1"]))
    toc = ["1. The Plan", "2. Architecture", "3. Quick Start & Usage",
           "4. The Inter-Agent Protocol", "5. free-claude-code Routing", "6. Full Source"]
    for t in toc:
        story.append(Paragraph(t, S["toc"]))
    story.append(PageBreak())

    # 1. plan
    story.append(Paragraph("1. The Plan", S["h1"]))
    story += section_from_file(ROOT / "PLAN.md", W)
    story.append(PageBreak())

    # 2. architecture
    story.append(Paragraph("2. Architecture", S["h1"]))
    story += section_from_file(ROOT / "ARCHITECTURE.md", W)
    story.append(PageBreak())

    # 3. usage
    story.append(Paragraph("3. Quick Start & Usage", S["h1"]))
    usage = """## Install
```bash
cd triad_project
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Configure keys
```bash
python -m triad setup          # masked prompts, live validation, safe save
python -m triad setup --all    # also NVIDIA NIM / Groq / OpenRouter
```
Keys auto-load on startup — no `source` needed. Add or fix one mid-session with `/keys`.

## Run
```bash
python -m triad                 # parallel mode
python -m triad --mode council
python -m triad --mode relay
```

## Modes
- **parallel** — same task to all three at once, independent answers side by side.
- **relay** — agents work in sequence, each building on the previous output.
- **council** — all answer, then a chair agent synthesizes the best single answer.

## Commands
```text
/mode parallel|relay|council   switch collaboration mode
/protocol on|off               compact handoffs in relay/council (saves tokens)
/skill <name> [agent|all]      apply a skill to an agent
/skills                        list available skills
/agents                        list active agents + models
/keys                          add or fix an API key
/reset                         clear history
/save [file.md]                save transcript
/help    /quit
```

## Skills
Markdown files in `skills/` with frontmatter (`name`, `description`, `agents`). Applying
one appends its body to that agent's system prompt; stack as many as you like. Seven ship
with the project: planner, architect, implementer, code-reviewer, red-team, synthesizer, concise.
"""
    story += md_to_flowables(usage, W)
    story.append(PageBreak())

    # 4. protocol
    story.append(Paragraph("4. The Inter-Agent Protocol", S["h1"]))
    proto = """Relay and council normally re-send the whole growing transcript on every hop —
that's the real token waste. With `/protocol on`, agents emit a terse, structured handoff
and only the digest is passed forward. The full output is kept in a reference store so the
human transcript loses nothing. It stays in-distribution (models already understand the
format, so capability holds) and human-readable (you keep oversight).

## Handoff format
```text
@goal <one line: what you're solving>
@find
- <terse finding or contribution>
- <terse finding>
@conf <0.0-1.0>
@next <what's open / who should act / 'done'>
```

## Why not a brand-new compressed language
A truly novel notation pushes models out-of-distribution: you get a slower, dumber agent,
and you can no longer read what they're saying — bad for a human-in-the-loop design. Real
machine-to-machine compression (latent vectors / "neuralese") needs models that share a
representation space, which you only get with local models you run yourself — never across
the OpenAI/Anthropic/Gemini text APIs. So the protocol is a compact *schema* the models
already know, plus reference-passing instead of re-quoting. After a relay or council run it
prints how much handoff context that saved; the saving grows with conversation length.
"""
    story += md_to_flowables(proto, W)
    story.append(PageBreak())

    # 5. routing
    story.append(Paragraph("5. free-claude-code Routing", S["h1"]))
    story += section_from_file(ROOT / "config" / "README.md", W)
    story.append(Paragraph("config/free-claude-code.env", S["h3"]))
    story.append(code_flowable((ROOT / "config" / "free-claude-code.env").read_text(), W))
    story.append(PageBreak())

    # 6. source
    story.append(Paragraph("6. Full Source", S["h1"]))
    story.append(Paragraph(
        "Complete snapshot — enough to reconstruct or hand to any AI for full context.",
        S["body"]))
    src_files = [
        "triad/config.py", "triad/agents.py", "triad/skills.py", "triad/ui.py",
        "triad/orchestrator.py", "triad/protocol.py", "triad/vault.py", "triad/sandbox.py",
        "triad/oracle.py", "triad/dotenv.py",
        "triad/keychain.py", "triad/setup_keys.py", "triad/cli.py",
        "triad/__main__.py", "triad/__init__.py",
        "requirements.txt", ".env.example",
        "skills/planner.md", "skills/architect.md", "skills/implementer.md",
        "skills/code-reviewer.md", "skills/red-team.md", "skills/synthesizer.md",
        "skills/concise.md",
    ]
    story.append(Paragraph("Python", S["h2"]))
    for rel in src_files:
        if (ROOT / rel).exists():
            story += source_block(rel, W)

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print("wrote", OUT, "size", OUT.stat().st_size)


if __name__ == "__main__":
    build()
