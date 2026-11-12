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


def _scan_inline_dest(text: str, i: int) -> str:
    """Given the index right after a link/image `[label](`, decide whether the inline
    destination renders. Models the CommonMark inline-link grammar closely enough to
    catch the two catastrophic shapes without false positives:
        after '(':  [ws] destination [ws title] [ws] ')'
    where destination is either `<...>` (angle, no raw newline) or a bare run of
    non-whitespace with BALANCED parens, and title is "...", '...', or (...) (may span
    newlines). Returns 'ok', 'split' (a newline breaks the destination token), or
    'unclosed' (no closing ')').
    """
    n = len(text)

    def skip_ws(j: int) -> int:
        while j < n and text[j] in " \t\n":
            j += 1
        return j

    j = skip_ws(i)                        # leading ws (incl newline) before dest is legal
    if j >= n:
        return "unclosed"

    # Angle-bracket destination <...> — a raw newline inside it is invalid, but a
    # backslash-escaped '>' does NOT terminate it. Scan honoring escapes.
    if text[j] == "<":
        k = j + 1
        while k < n and text[k] not in ">\n":
            if text[k] == "\\" and k + 1 < n:
                # An escaped char doesn't terminate the angle dest — but an escaped
                # NEWLINE is still a raw newline inside <...>, which is invalid.
                if text[k + 1] == "\n":
                    return "split"
                k += 2
            else:
                k += 1
        if k >= n:
            return "unclosed"
        if text[k] == "\n":
            return "split"
        j = k + 1
    else:
        # Bare destination: run of non-whitespace with balanced parens. It ends at the
        # first whitespace, or at the ')' that closes the link (depth 0).
        depth = 0
        while j < n:
            c = text[j]
            if c == "\\" and j + 1 < n:
                # An escaped char doesn't end the destination — but an escaped NEWLINE
                # is still a raw line ending, which GFM/CommonMark forbid in a bare
                # destination (GitHub's cmark-gfm won't render it). Treat as split.
                if text[j + 1] == "\n":
                    return "split"
                j += 2
                continue
            if c in " \t\n":
                break                     # whitespace terminates the destination token
            if c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    return "ok"           # this ')' closes the link — complete
                depth -= 1
            j += 1
        else:
            return "unclosed"             # ran off the end with no closing ')'
        # Whitespace ended the bare destination. A bare dest must have BALANCED parens;
        # if a '(' is still open, the destination is malformed and won't render.
        if depth > 0:
            return "split"

    # Destination token ended at whitespace. Skip it (a single newline here is legal),
    # then the next non-whitespace must be the close ')' or a title opener.
    j = skip_ws(j)
    if j >= n:
        return "unclosed"
    c = text[j]
    if c == ")":
        return "ok"
    if c in "\"'(":
        # A title is present. It ends at its matching delimiter — `"..."`, `'...'`, or
        # a balanced `(...)`. Titles may span newlines. After the title, whitespace is
        # allowed and then the link's OWN closing ')' MUST follow; otherwise the link
        # is unclosed (e.g. `[x](foo (title)` — the sole ')' closes the title, not the
        # link). Walk the title body honoring escapes.
        openc = c
        k = j + 1
        if openc == "(":
            # A parenthesized title must NOT contain an unescaped '(' — CommonMark
            # forbids it, so `(a (b) c)` is not a valid title and the link won't render.
            while k < n:
                ch = text[k]
                if ch == "\\" and k + 1 < n:
                    k += 2
                    continue
                if ch == "(":
                    return "unclosed"      # nested unescaped '(' → invalid title
                if ch == ")":
                    break
                k += 1
            if k >= n:
                return "unclosed"          # parenthesized title never closed
        else:
            closec = openc                 # matching " or '
            while k < n and text[k] != closec:
                if text[k] == "\\" and k + 1 < n:
                    k += 2
                else:
                    k += 1
            if k >= n:
                return "unclosed"          # quoted title never closed
        # k is at the title's closing delimiter. After it: optional ws then the link ')'.
        k = skip_ws(k + 1)
        if k < n and text[k] == ")":
            return "ok"
        return "unclosed"
    # Anything else after the destination + whitespace is bare text → the inline link is
    # malformed and renders as literal characters. This is the catastrophic split, e.g.
    # `[x](https://exa\nmple.com)` or `[x](url\nmore)`.
    return "split"


def check_file(path: Path) -> list[str]:
    problems: list[str] = []
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = _blank_code(raw)

    # Locate each inline link/image opener `[label](` (label has no unescaped ] or newline).
    for m in re.finditer(r"!?\[(?:[^\]\n\\]|\\.)*\]\(", text):
        line_no = text.count("\n", 0, m.start()) + 1
        verdict = _scan_inline_dest(text, m.end())
        if verdict == "split":
            problems.append(
                f"{path}:{line_no}: newline inside a link destination — the link will not render")
        elif verdict == "unclosed":
            problems.append(
                f"{path}:{line_no}: link/image destination never closed with ')' — renders broken")
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
