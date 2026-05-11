#!/usr/bin/env python3
"""
Refresh CONTRIBUTORS.md from CHANGELOG.md attribution lines.

Why this script exists
----------------------
CONTRIBUTORS.md is the canonical credit roll. We re-derive it on each release
rather than hand-editing it because:

  * we ship a lot of contributor PRs as squashed batch releases — the
    `git log --author=...` count alone undercounts batched contributors;
  * we sometimes close a contributor PR (not merge it) but ship the same
    fix via a follow-up integration branch. The contributor still gets
    credit — closed-but-incorporated work counts the same as merged PRs.
    GitHub PR state alone cannot tell you that; CHANGELOG attribution
    can. This script reads attributions, not PR state.

The four canonical attribution shapes in CHANGELOG.md
-----------------------------------------------------

  - **PR #2052** by @franksong2702 — ...
  - PR #2052 by @franksong2702 — ...
  - @KingBoyAndGirl — PR #1268
  - @KingBoyAndGirl — Closes #1247
  - By @24601. [#962] — ...
  - [#1040 @24601]
  - (by @frap129, PR #1199)
  - (PR #275, @gabogabucho)
  - **#1942** (franksong2702 — synchronous mutex for #1937)
  - by @user (PR #1234)
  - @user (PR #1234)
  - #1234 from @user

Run
---
    python scripts/refresh_contributors.py [--dry-run]

Writes the updated CONTRIBUTORS.md in place (plus the README contributors
section block printed to stdout). The script also unions in any handle
already present in CONTRIBUTORS.md but absent from the changelog so that
old releases — whose CHANGELOG entries pre-date the standard attribution
shapes — don't lose their credit when we regenerate.
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
CONTRIBUTORS = REPO_ROOT / "CONTRIBUTORS.md"

# Maintainer accounts — never credited as community contributors.
MAINTAINERS = {"nesquena", "nesquena-hermes"}

# Reserved English words that can match the user-group of an attribution
# pattern but are obviously not GitHub handles. Without this filter,
# "and @other" / "by @user" inside loose prose can pollute the list.
JUNK_USERS = {"and", "or", "by", "pr", "closes", "fixes", "see", "the",
              "from", "via", "one", "two", "co", "authored"}

# Attribution patterns. Each has named groups `pr` (digits) and `u`
# (handle). Order matters only for diagnostics — every pattern runs.
ATTRIBUTION_PATTERNS = [
    r"\*\*PR\s*#(?P<pr>\d+)\*\*\s+by\s+@?(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})",
    r"PR\s*#(?P<pr>\d+)\s+by\s+@?(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})",
    r"@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s+[—\-]\s+PR\s*#(?P<pr>\d+)",
    r"@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s+[—\-]\s+Closes\s+#(?P<pr>\d+)",
    r"\(\s*by\s+@?(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s*[,\s]+PR\s*#(?P<pr>\d+)\s*\)",
    r"\(\s*PR\s*#(?P<pr>\d+)\s*[,\s]+@?(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s*\)",
    r"\*\*#(?P<pr>\d+)\*\*\s*\(\s*(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s+—",
    r"by\s+@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s*\(\s*PR\s*#(?P<pr>\d+)\s*\)",
    r"@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s*\(\s*PR\s*#(?P<pr>\d+)\s*\)",
    r"#(?P<pr>\d+)\s+[—\-]\s+@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})",
    r"@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s*,\s*PR\s*#(?P<pr>\d+)",
    r"PR\s*#(?P<pr>\d+)\s*\(\s*@?(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s*\)",
    r"#(?P<pr>\d+)\s+from\s+@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})",
    r"By\s+@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\.\s*\[#(?P<pr>\d+)\]",
    r"\[#(?P<pr>\d+)\s+@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\]",
    r"Co-authored\s+by\s+@(?P<u>[A-Za-z0-9][A-Za-z0-9\-_]{0,38})\s+\(?PR\s*#(?P<pr>\d+)\)?",
]
COMPILED_PATTERNS = [re.compile(p, re.IGNORECASE) for p in ATTRIBUTION_PATTERNS]

RELEASE_RE = re.compile(r"^## \[(v[\d\.]+)\] — (\d{4}-\d{2}-\d{2})", re.MULTILINE)


def parse_changelog(text: str):
    """Return (contrib, pr_first_release, pr_release).

    contrib: handle -> set of PR numbers credited to that handle.
    pr_first_release: (handle, pr) -> (tag, iso_date) of earliest mention.
    pr_release: (handle, pr) -> list of (tag, iso_date) for every mention.
    """
    contrib = defaultdict(set)
    pr_first_release: dict = {}
    pr_release = defaultdict(list)

    release_iter = list(RELEASE_RE.finditer(text))
    sections = []
    for i, m in enumerate(release_iter):
        end = release_iter[i + 1].start() if i + 1 < len(release_iter) else len(text)
        sections.append((m.group(1), m.group(2), text[m.start():end]))

    # Walk releases oldest-to-newest so pr_first_release records the
    # earliest mention of any (handle, pr) pair.
    for tag, date, section in reversed(sections):
        for pat in COMPILED_PATTERNS:
            for m in pat.finditer(section):
                user = m.group("u")
                pr_num = int(m.group("pr"))
                if user.lower() in JUNK_USERS:
                    continue
                if user.startswith("-") or user.endswith("-"):
                    continue
                if len(user) > 39:
                    continue
                contrib[user].add(pr_num)
                key = (user, pr_num)
                pr_first_release.setdefault(key, (tag, date))
                pr_release[key].append((tag, date))
    return contrib, pr_first_release, pr_release


def parse_existing_contributors(text: str):
    """Return {handle: count} from the previous CONTRIBUTORS.md.

    Used to union in old-release contributors whose CHANGELOG attribution
    pre-dates our standard shapes.
    """
    counts = {}
    # Top-tier table rows: "| 1 | [@user](url) | 22 | ..."
    top_re = re.compile(
        r"\|\s*\d+\s*\|\s*\[@([A-Za-z0-9\-_]+)\]\([^)]+\)\s*\|\s*(\d+)\s*\|"
    )
    for m in top_re.finditer(text):
        counts[m.group(1)] = int(m.group(2))
    # Sustained rows: "| [@user](url) | 4 | ... |"
    sust_re = re.compile(
        r"\|\s*\[@([A-Za-z0-9\-_]+)\]\([^)]+\)\s*\|\s*(\d+)\s*\|"
    )
    for m in sust_re.finditer(text):
        counts.setdefault(m.group(1), int(m.group(2)))
    # Two-PR section: handle list under "## Two-PR contributors".
    in_two = re.search(r"## Two-PR contributors\s*\n\n(.*?)\n##", text, re.DOTALL)
    if in_two:
        for m in re.finditer(r"\[@([A-Za-z0-9\-_]+)\]\([^)]+\)", in_two.group(1)):
            counts.setdefault(m.group(1), 2)
    # Single-PR section.
    in_one = re.search(
        r"## Single-PR contributors\s*\n\n.*?\n\n(.*?)(\n---|\n##)",
        text,
        re.DOTALL,
    )
    if in_one:
        for m in re.finditer(r"\[@([A-Za-z0-9\-_]+)\]\([^)]+\)", in_one.group(1)):
            counts.setdefault(m.group(1), 1)
    # Catch-all (special-thanks blocks, etc.)
    for m in re.finditer(r"\[@([A-Za-z0-9\-_]+)\]\([^)]+\)", text):
        counts.setdefault(m.group(1), 1)
    return counts


def parse_existing_dates(text: str):
    """Return {handle: ((first_tag, first_date), (last_tag, last_date))} for top-tier rows."""
    row_re = re.compile(
        r"\|\s*\d+\s*\|\s*\[@([A-Za-z0-9\-_]+)\]\([^)]+\)\s*\|\s*\d+\s*\|"
        r"\s*`(v[\d\.]+)`\s*(\d{4}-\d{2}-\d{2})\s*\|"
        r"\s*`(v[\d\.]+)`\s*(\d{4}-\d{2}-\d{2})\s*\|"
    )
    out = {}
    for m in row_re.finditer(text):
        user, fv, fd, lv, ld = m.groups()
        out[user] = ((fv, fd), (lv, ld))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print results to stdout; do not write CONTRIBUTORS.md.")
    args = parser.parse_args()

    text = CHANGELOG.read_text(encoding="utf-8")
    existing = CONTRIBUTORS.read_text(encoding="utf-8")

    contrib, pr_first_release, pr_release = parse_changelog(text)
    existing_counts = parse_existing_contributors(existing)
    existing_dates = parse_existing_dates(existing)

    users = (set(existing_counts) | set(contrib)) - MAINTAINERS
    merged = {}
    for user in users:
        cl_count = len(contrib.get(user, set()))
        ex_count = existing_counts.get(user, 0)
        final = max(cl_count, ex_count)

        first = last = None
        if user in contrib:
            firsts = [pr_first_release[(user, p)] for p in contrib[user]]
            lasts = []
            for p in contrib[user]:
                lasts.extend(pr_release[(user, p)])
            first = min(firsts, key=lambda x: x[1])
            last = max(lasts, key=lambda x: x[1])
        if user in existing_dates:
            ef, el = existing_dates[user]
            if first is None or ef[1] < first[1]:
                first = ef
            if last is None or el[1] > last[1]:
                last = el

        merged[user] = {
            "count": final,
            "first": first,
            "last": last,
            "prs": sorted(contrib.get(user, set())),
        }

    ranked = sorted(merged.items(), key=lambda kv: (-kv[1]["count"], kv[0].lower()))
    total = len(merged)
    sum_prs = sum(v["count"] for v in merged.values())

    print(f"Total external contributors: {total}")
    print(f"Total PR attributions: {sum_prs}")
    print(f"Top 10:")
    for user, v in ranked[:10]:
        print(f"  {v['count']:>3} @{user}")


if __name__ == "__main__":
    main()
