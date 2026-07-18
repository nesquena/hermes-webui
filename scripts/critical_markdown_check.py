#!/usr/bin/env python3
"""critical_markdown_check.py — catch ONLY catastrophic Markdown rendering breaks.

Deliberately minimal (Nathan, 2026-07-18): "I don't want it to be so overdone...
only pretty critical, borderline catastrophic Markdown issues, like there is a new
line in a link that would prevent the link from rendering." This is NOT a style
linter and NOT a prose scanner — it does not care about line length, headings,
trailing spaces, list markers, or whether a doc happens to describe a `<script>`
tag or a `javascript:` URL in its prose. Those are legitimate technical writing.

It flags ONLY two things that genuinely break how the document renders, and which
cannot occur by accident in normal prose:

  1. A Markdown link/image whose destination is split across a newline —
     `[text](\nhttps://x)` or a `](` with a newline before its closing `)`.
     Markdown forbids a newline inside an inline-link destination, so the link
     renders as literal text. This is the exact case Nathan named.
  2. A single-line inline link/image whose destination is never closed —
     `[label](https://x` with no `)` before end-of-line. Renders broken.

Code spans (fenced ``` blocks and inline `code`) are blanked first, so an example
of "bad" Markdown shown INSIDE a code block is intentional and never flagged.

Exit 1 (blocks merge) only on one of the two defects above. Anything ambiguous is
NOT flagged — a false positive that blocks a docs PR is worse than missing a
cosmetic issue. Broken *external* links (dead URLs) are handled separately by the
lychee link-check step in the workflow, not here.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path


def _strip_code_spans(text: str) -> str:
    """Blank fenced code blocks and inline `code`, preserving line structure."""
    out = []
    in_fence = False
    fence = ""
    for line in text.split("\n"):
        stripped = line.lstrip()
        if not in_fence and (stripped.startswith("```") or stripped.startswith("~~~")):
            in_fence = True
            fence = stripped[:3]
            out.append("")
            continue
        if in_fence:
            if stripped.startswith(fence):
                in_fence = False
            out.append("")
            continue
        out.append(re.sub(r"`[^`]*`", lambda m: " " * len(m.group(0)), line))
    return "\n".join(out)


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = _strip_code_spans(raw)

    # Find each inline link/image opener `[label](` and inspect its destination.
    # We require a real `[...](` shape (label immediately followed by `(`) so plain
    # prose containing `](` is not mistaken for a link.
    for m in re.finditer(r"(!?)\[[^\]\n]*\]\(", text):
        open_pos = m.end()          # char right after the `(`
        nl = text.find("\n", open_pos)
        close = text.find(")", open_pos)
        line_no = text.count("\n", 0, m.start()) + 1
        if close == -1 or (nl != -1 and nl < close):
            # newline (or EOF) before the closing paren → destination broken.
            # Distinguish the two messages for a clearer report.
            if nl != -1 and nl < (close if close != -1 else nl + 1):
                problems.append(
                    f"{path}:{line_no}: link/image destination split across a newline — "
                    f"the link will not render")
            else:
                problems.append(
                    f"{path}:{line_no}: link/image destination never closed with ')' — "
                    f"renders broken")
    return problems


def main(argv: list[str]) -> int:
    files = [Path(a) for a in argv[1:] if a.strip()]
    if not files:
        print("critical_markdown_check: no files to check (ok)")
        return 0
    all_problems: list[str] = []
    scanned = 0
    for f in files:
        if not f.is_file():
            continue
        scanned += 1
        try:
            all_problems.extend(check_file(f))
        except Exception as e:  # never crash the gate on an odd file
            print(f"warning: could not scan {f}: {e!r}")
    if all_problems:
        print("Critical Markdown problems (these block the merge):")
        for p in all_problems:
            print(f"  ✗ {p}")
        return 1
    print(f"critical_markdown_check: {scanned} file(s) OK — no catastrophic Markdown issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
