#!/usr/bin/env python3
"""Forward-looking ruff lint gate for hermes-webui.

The Python twin of the ESLint runtime guard (``npm run lint:runtime``). It runs the
curated ``[tool.ruff]`` ruleset from ``pyproject.toml`` (E9 + F + B) but enforces it
**only on new / changed code**, so it keeps incoming PRs clean without demanding a
big-bang reformat of the existing tree (which still carries a cosmetic
unused-import backlog — see issue #3273).

Two modes:

  --diff [BASE]   Line-scoped gate (default mode). Only report findings on lines
                  that this branch ADDED or MODIFIED relative to the merge-base
                  with BASE (default: origin/master). Editing a legacy file that
                  has pre-existing violations elsewhere is safe — only your own
                  new lines are gated. Exit 1 if any new finding, else 0.

  --all           Whole-tree report (informational). Runs the curated ruleset over
                  every tracked .py file and prints the backlog. Exit 0 always
                  unless --strict is also passed. Useful for tracking the cleanup
                  progress of #3273; NOT used as a release blocker.

Usage::

    python3 scripts/ruff_lint.py --diff origin/master     # the CI / pre-release gate
    python3 scripts/ruff_lint.py --all                    # backlog report
    python3 scripts/ruff_lint.py --all --strict           # fail on ANY tree finding

ruff is resolved from PATH, or run via ``uvx ruff`` / ``python -m ruff`` as a
fallback. If ruff cannot be found at all the gate prints a notice and exits 0 (a
dev without ruff is not blocked; CI installs ruff so enforcement still holds there —
same contract as the ESLint guard).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, **kw
    )


def _ruff_cmd() -> list[str] | None:
    """Return the argv prefix that invokes ruff, or None if unavailable."""
    if shutil.which("ruff"):
        return ["ruff"]
    # `python -m ruff` works when ruff is pip-installed in the active interpreter.
    probe = _run([sys.executable, "-m", "ruff", "--version"])
    if probe.returncode == 0:
        return [sys.executable, "-m", "ruff"]
    # uvx pulls a pinned ruff on demand (used in local dev boxes without a global ruff).
    if shutil.which("uvx"):
        probe = _run(["uvx", "ruff", "--version"])
        if probe.returncode == 0:
            return ["uvx", "ruff"]
    return None


def _changed_py_files(base: str) -> tuple[str, list[str]]:
    """Resolve the merge-base with `base` and return (merge_base, changed .py files)."""
    mb = _run(["git", "merge-base", base, "HEAD"])
    merge_base = mb.stdout.strip() if mb.returncode == 0 and mb.stdout.strip() else base
    diff = _run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", merge_base, "HEAD"]
    )
    files = [
        f
        for f in diff.stdout.splitlines()
        if f.endswith(".py") and os.path.exists(os.path.join(REPO_ROOT, f))
    ]
    return merge_base, files


_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _added_lines(merge_base: str, path: str) -> set[int]:
    """Set of NEW-file line numbers added or modified in `path` since merge_base."""
    diff = _run(["git", "diff", "--unified=0", merge_base, "HEAD", "--", path])
    added: set[int] = set()
    new_ln = 0
    for line in diff.stdout.splitlines():
        m = _HUNK_RE.match(line)
        if m:
            new_ln = int(m.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.add(new_ln)
            new_ln += 1
        elif line.startswith("-") and not line.startswith("---"):
            # deletion: new-file pointer does not advance
            continue
        elif not line.startswith(("@@", "diff ", "index ", "--- ", "+++ ")):
            new_ln += 1
    return added


def _ruff_check_json(ruff: list[str], files: list[str]) -> list[dict]:
    """Run ruff over `files` with the project config, return parsed JSON findings."""
    if not files:
        return []
    proc = _run(ruff + ["check", "--output-format", "json", "--no-cache", *files])
    out = proc.stdout.strip()
    if not out:
        return []
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        sys.stderr.write(
            "ruff_lint: could not parse ruff JSON output:\n" + proc.stdout + proc.stderr
        )
        # Be conservative: surface as a failure so a broken invocation never
        # silently green-lights a PR.
        raise


def _print_findings(findings: list[dict]) -> None:
    by_file: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        by_file[f.get("filename", "?")].append(f)
    for fname in sorted(by_file):
        rel = os.path.relpath(fname, REPO_ROOT)
        for f in sorted(by_file[fname], key=lambda x: (x["location"]["row"], x["location"]["column"])):
            loc = f["location"]
            code = f.get("code") or "?"
            msg = f.get("message", "").strip()
            print(f"  {rel}:{loc['row']}:{loc['column']}  {code}  {msg}")


def run_diff(base: str) -> int:
    ruff = _ruff_cmd()
    if ruff is None:
        print("ruff_lint: ruff not found on PATH — skipping (CI installs ruff). OK.")
        return 0
    merge_base, files = _changed_py_files(base)
    if not files:
        print(f"ruff_lint: no changed .py files vs {base} ({merge_base[:12]}). OK.")
        return 0

    all_findings = _ruff_check_json(ruff, files)
    # Build the added-line map once per file, intersect.
    added_map = {f: _added_lines(merge_base, f) for f in files}
    new_findings = []
    for finding in all_findings:
        rel = os.path.relpath(finding.get("filename", ""), REPO_ROOT)
        row = finding.get("location", {}).get("row")
        if rel in added_map and row in added_map[rel]:
            new_findings.append(finding)

    print(
        f"ruff_lint (diff vs {base}): {len(files)} changed .py file(s), "
        f"{len(all_findings)} total finding(s) in them, "
        f"{len(new_findings)} on added/modified lines."
    )
    if new_findings:
        print("\nNew ruff violations introduced by this change:\n")
        _print_findings(new_findings)
        print(
            "\nThe curated ruff gate (E9+F+B) flags NEW code only. Fix the lines above, "
            "or — if a finding is a genuine false positive — add a scoped "
            "`# noqa: <CODE>` with a one-line reason. Config: pyproject.toml [tool.ruff]."
        )
        return 1
    print("ruff_lint: no new violations on added/modified lines. OK.")
    return 0


def run_all(strict: bool) -> int:
    ruff = _ruff_cmd()
    if ruff is None:
        print("ruff_lint: ruff not found on PATH — skipping. OK.")
        return 0
    proc = _run(ruff + ["check", "--statistics", "--no-cache", "."])
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    if strict and proc.returncode != 0:
        return proc.returncode
    print(
        "\nruff_lint --all is informational (existing-tree backlog tracked in #3273). "
        "Pass --strict to fail on any finding."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--diff",
        nargs="?",
        const="origin/master",
        metavar="BASE",
        help="Line-scoped gate vs merge-base with BASE (default origin/master).",
    )
    g.add_argument("--all", action="store_true", help="Whole-tree backlog report.")
    p.add_argument("--strict", action="store_true", help="With --all: fail on any finding.")
    args = p.parse_args(argv)

    if args.all:
        return run_all(args.strict)
    base = args.diff or "origin/master"
    return run_diff(base)


if __name__ == "__main__":
    raise SystemExit(main())
