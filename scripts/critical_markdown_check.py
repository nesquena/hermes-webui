#!/usr/bin/env python3
"""critical_markdown_check.py — catch ONLY catastrophic Markdown rendering breaks.

Deliberately minimal (Nathan, 2026-07-18): "only pretty critical, borderline
catastrophic Markdown issues, like there is a new line in a link that would prevent
the link from rendering." This is NOT a style linter and NOT a prose scanner. It
flags ONLY two constructs that genuinely break how the document renders, verified
against the CommonMark reference parser (markdown-it-py), and which do not occur in
normal prose:

  1. A newline INSIDE an inline-link destination TOKEN — `[label](https://exa\\nmple.com)`.
     The destination is a single token; a newline splitting it (non-space, newline,
     non-space, before any closing `)`) makes CommonMark NOT render the link.
     IMPORTANT: a newline in the *whitespace* around the destination —
     `[label](\\nhttps://x)`, `[label](https://x\\n)`, or inside a `"title"` — is
     LEGAL CommonMark and renders fine, so those are NOT flagged (verified).
  2. A single-line inline link/image whose destination is never closed —
     `[label](https://x` with no `)` before end of line. Renders broken.

Code spans (fenced ``` / ~~~ blocks, 4-space indented code, and inline `code`) are
blanked first so an example of "bad" Markdown shown inside code is never flagged.

Exit 1 only on one of the two defects above. Anything ambiguous is NOT flagged — a
false positive on a docs PR is worse than missing a cosmetic issue. Dead external
links are handled separately by the lychee step, not here.
"""
from __future__ import annotations
import re
import sys
from pathlib import Path


def _blank_code(text: str) -> str:
    """Blank fenced blocks, 4-space indented code, and inline code — keep line count."""
    out = []
    in_fence = False
    fence_marker = ""
    for line in text.split("\n"):
        stripped = line.lstrip()
        # Fence open/close. A closing fence must be at least as long and same char.
        m = re.match(r"^(\s*)(`{3,}|~{3,})", line)
        if not in_fence and m:
            in_fence = True
            fence_marker = m.group(2)[0] * len(m.group(2))
            out.append("")
            continue
        if in_fence:
            cm = re.match(r"^\s*(`{3,}|~{3,})", line)
            if cm and cm.group(1)[0] == fence_marker[0] and len(cm.group(1)) >= len(fence_marker):
                in_fence = False
            out.append("")
            continue
        # 4-space (or tab) indented code line → blank it.
        if re.match(r"^(?: {4}|\t)", line):
            out.append("")
            continue
        # inline `code` (including multi-backtick) → blank spans on this line.
        out.append(re.sub(r"(`+)(?:.*?)\1", lambda mm: " " * len(mm.group(0)), line))
    return "\n".join(out)


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = _blank_code(raw)

    # Locate each inline link/image opener `[label](` (label has no unescaped ] or newline).
    for m in re.finditer(r"!?\[(?:[^\]\n\\]|\\.)*\]\(", text):
        dest_start = m.end()          # first char of the destination
        close = text.find(")", dest_start)
        nl = text.find("\n", dest_start)
        line_no = text.count("\n", 0, m.start()) + 1

        # Case A — the close paren is on THIS line: fully-formed, nothing to flag.
        if close != -1 and (nl == -1 or close < nl):
            continue

        # From here the ')' (if any) is on a later line, OR there's no ')' at all.
        if nl == -1:
            # No newline and (from Case A) no ')' → genuinely unclosed at EOF.
            problems.append(
                f"{path}:{line_no}: link/image destination never closed with ')' — renders broken")
            continue

        # A newline appears before any ')'. Once a space/newline ends the destination
        # token, CommonMark expects either the closing ')' or a "title" (in quotes)
        # then ')'. So after the newline, the next non-space content must be a title
        # (`"`, `'`, or `(`) or the close `)`. If instead it's bare text, the inline
        # link is malformed and does NOT render — that's the break we flag.
        #
        # Cases:
        #   [x](url\n)             -> after nl: ')'      -> valid   (skip)
        #   [x](url\n"title")      -> after nl: '"title' -> valid   (skip)
        #   [x](\nurl)            -> dest starts next line-> valid   (skip)
        #   [x](url\nmore)        -> after nl: 'more'    -> BROKEN  (flag)
        #   [x](url<EOF, no ')')  -> unclosed            -> BROKEN  (flag)
        rest = text[nl + 1:]
        rest_stripped = rest.lstrip()
        seg = text[dest_start:nl]

        # If a title quote has already opened before the newline (e.g.
        # `[x](url "multi\nline title")`), the newline is INSIDE the title string,
        # which is legal CommonMark. Don't treat it as a destination break.
        if ('"' in seg) or ("'" in seg):
            continue

        # Sub-case: the destination itself hasn't started yet (all whitespace before nl)
        # AND resumes on the next line as a normal dest — legal.
        if seg.strip() == "" and rest_stripped[:1] not in ("", ")"):
            continue

        # Is there any ')' at all after the newline? If not, it's unclosed -> broken.
        if close == -1:
            problems.append(
                f"{path}:{line_no}: link/image destination never closed with ')' — renders broken")
            continue

        # There is a ')' later. Valid continuations after the newline: close ')',
        # or a title opener (" ' or ( ). Anything else (bare text) => broken.
        nxt = rest_stripped[:1]
        if nxt in (")", '"', "'", "("):
            continue                      # legal separator before title/close
        # bare non-space text after the newline before the close => destination
        # token is split across the line => the link will not render.
        problems.append(
            f"{path}:{line_no}: newline inside a link destination — the link will not render")
        # else: whitespace ended the token before the newline, or the newline is
        # immediately followed by ')'/space (trailing) → legal CommonMark, skip.
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
        print("Critical Markdown problems (fix these — they break rendering):")
        for p in all_problems:
            print(f"  ✗ {p}")
        return 1
    print(f"critical_markdown_check: {scanned} file(s) OK — no catastrophic Markdown issues")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
