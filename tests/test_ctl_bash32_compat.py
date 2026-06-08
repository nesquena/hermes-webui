"""Regression tests pinning bash 3.2 compatibility patterns in ctl.sh.

macOS still ships bash 3.2 as the default ``/usr/bin/bash``. Under
``set -euo pipefail`` (which ctl.sh sets at the top of the file), bash 3.2
treats *empty array expansion* as referencing an unbound variable and aborts
with ``preserved[@]: unbound variable`` (or equivalent). Bash 4+ silently
handles empty arrays. We can't realistically run the CI suite under bash 3.2,
so these are static-pattern assertions on the source file -- if a future PR
introduces a raw ``"${arr[@]}"`` expansion without the established guards,
this test fails fast.

Two guard patterns are used in ctl.sh:

1. Length-guarded ``for`` loop::

       if [[ ${#preserved[@]} -gt 0 ]]; then
         for assignment in "${preserved[@]}"; do
           export "${assignment}"
         done
       fi

   Used when the loop body has side effects we want to skip when empty.
   (PR #2117 introduced this pattern at the ``preserved`` site.)

2. Inline ``${arr[@]+...}`` expansion::

       exec ... ${CTL_BOOTSTRAP_ARGS[@]+"${CTL_BOOTSTRAP_ARGS[@]}"}

   Used when we want to pass-through the array to a command and have the
   expansion produce nothing when empty. (PR ``025f137f`` introduced this
   pattern at the ``CTL_BOOTSTRAP_ARGS`` site.)

Either pattern is acceptable -- a raw ``"${arr[@]}"`` without one of them is
not.
"""

from __future__ import annotations

import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
CTL_SH = REPO_ROOT / "ctl.sh"


def _read_ctl() -> str:
    return CTL_SH.read_text(encoding="utf-8")


def test_ctl_sh_sets_strict_mode() -> None:
    """ctl.sh must keep ``set -euo pipefail`` -- the bug class only triggers under -u."""
    src = _read_ctl()
    assert "set -euo pipefail" in src, (
        "ctl.sh must use strict-mode `set -euo pipefail`; otherwise the bash 3.2 "
        "empty-array guards we're pinning here are unnecessary and the file lost "
        "its bug-class coverage."
    )


def test_preserved_array_is_length_guarded_before_iteration() -> None:
    """The dotenv-preserve loop must guard against empty `preserved=()` on bash 3.2.

    PR #2117 (ayushere) — guards the iteration with
    ``if [[ ${#preserved[@]} -gt 0 ]]; then ... fi``. Without the guard, bash
    3.2 on macOS aborts ``ctl.sh start`` before bootstrap even launches.
    """
    src = _read_ctl()
    # Must have the length guard somewhere upstream of the for-loop iteration.
    guarded = re.search(
        r"if\s+\[\[\s+\$\{#preserved\[@\]\}\s+-gt\s+0\s+\]\];\s*then\s*"
        r"\s*for\s+\w+\s+in\s+\"\$\{preserved\[@\]\}\"",
        src,
    )
    assert guarded, (
        "Raw `for assignment in \"${preserved[@]}\"` iteration crashes under "
        "bash 3.2 + set -u when no preserved env keys overlap with .env. "
        "Wrap the loop in `if [[ ${#preserved[@]} -gt 0 ]]; then ... fi` "
        "(PR #2117)."
    )


def test_ctl_bootstrap_args_uses_plus_alternate_expansion() -> None:
    """The exec line must use ``${CTL_BOOTSTRAP_ARGS[@]+...}`` for empty-safe pass-through.

    PR ``025f137f`` — bash 3.2 + ``set -u`` treats ``"${CTL_BOOTSTRAP_ARGS[@]}"``
    as an unbound reference when the array is empty. The ``+alt`` parameter
    expansion produces nothing when unset and our quoted expansion otherwise,
    which is the canonical bash 3.2 / strict-mode pattern.
    """
    src = _read_ctl()
    has_plus_alt = re.search(
        r"\$\{CTL_BOOTSTRAP_ARGS\[@\]\+\"\$\{CTL_BOOTSTRAP_ARGS\[@\]\}\"\}",
        src,
    )
    assert has_plus_alt, (
        "exec line must use `${CTL_BOOTSTRAP_ARGS[@]+\"${CTL_BOOTSTRAP_ARGS[@]}\"}` "
        "so an empty CTL_BOOTSTRAP_ARGS doesn't trip bash 3.2 + set -u. "
        "See commit 025f137f."
    )


def test_no_array_iteration_without_guard_in_ctl() -> None:
    """Defense-in-depth: catch *any* future raw array expansion not protected by a guard.

    Whitelist the two known-safe sites (preserved + CTL_BOOTSTRAP_ARGS). Any
    other ``"${SOMETHING[@]}"`` expansion in ctl.sh should also use one of the
    two established empty-safe patterns; this test surfaces the new site so the
    author can decide which.
    """
    src = _read_ctl()
    # Match every quoted-all-elements expansion outside the +alt form.
    raw_expansions = re.findall(r'"\$\{([A-Za-z_][A-Za-z0-9_]*)\[@\]\}"', src)
    # Already-allowed names (each has its own dedicated regression test above).
    allowed = {"preserved", "CTL_BOOTSTRAP_ARGS"}
    new_unguarded = [name for name in raw_expansions if name not in allowed]
    assert not new_unguarded, (
        "New raw `\"${{{name}[@]}}\"` array expansion(s) appeared in ctl.sh: "
        "{names}. On bash 3.2 + `set -u` (macOS default), iterating or "
        "expanding an empty array aborts the script. Wrap iteration in "
        "`if [[ ${{#arr[@]}} -gt 0 ]]; then ... fi` (loop-side-effect "
        "pattern, see preserved at line ~54) or use "
        "`${{arr[@]+\"${{arr[@]}}\"}}` (pass-through pattern, see "
        "CTL_BOOTSTRAP_ARGS at line ~220) — then whitelist the name in "
        "`tests/test_ctl_bash32_compat.py::test_no_array_iteration_without_guard_in_ctl`."
    ).format(name=new_unguarded[0] if new_unguarded else "?", names=new_unguarded)


def test_no_bash4_plus_features_in_ctl() -> None:
    """Guard against accidental introduction of bash 4+ syntax in ctl.sh.

    macOS bash 3.2 does not support:
      - ``declare -A`` / ``local -A``  (associative arrays)
      - ``mapfile`` / ``readarray``    (line-into-array readers)
      - ``[[ -v VAR ]]``               (variable-existence test, bash 4.2+)
      - ``${var^^}`` / ``${var,,}``    (case toggle)

    A prior fix (commit 630981a0) replaced ``[[ -v ${key} ]]`` with
    ``[[ -n "${!key+x}" ]]`` specifically because of the macOS bash 3.2 issue.
    Keep that gain by pinning the absence of the bash 4+ patterns.
    """
    src = _read_ctl()

    forbidden = {
        "declare -A": r"\bdeclare\s+-A\b",
        "local -A": r"\blocal\s+-A\b",
        "mapfile": r"\bmapfile\b",
        "readarray": r"\breadarray\b",
        "[[ -v VAR ]]": r"\[\[\s*-v\s+",
        "${var^^}": r"\$\{[A-Za-z_][A-Za-z0-9_]*\^\^?\}",
        "${var,,}": r"\$\{[A-Za-z_][A-Za-z0-9_]*,,?\}",
    }
    found = [name for name, pat in forbidden.items() if re.search(pat, src)]
    assert not found, (
        f"ctl.sh introduced bash 4+ feature(s) {found} — these break macOS's "
        "default bash 3.2. Use a 3.2-compatible alternative; see commit "
        "630981a0 for the `-v` → `\"${!key+x}\"` substitution pattern."
    )
