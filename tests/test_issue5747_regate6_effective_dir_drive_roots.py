"""#5747 sixth round (review 2026-11-25T20:28:22Z): effective directory and
drive-letter roots.

Two path-resolution gaps remain after the credited sed-operand repair:

* E6a - an in-command directory change resolves the write against the supplied
  workdir. With the workspace as workdir, ``cd docs && sed -i 's/a/b/'
  notes.md`` records ``notes.md``, while the file the command actually wrote is
  ``docs/notes.md``; the open preview of that file therefore has no matching
  pending mutation. Supplying ``docs`` as workdir directly is a passing control.
* E6b - a drive-letter workspace drops an ordinary relative mutation. With
  workspace/workdir ``C:/project``, the same simple edit of ``notes.md`` records
  nothing: ``_collapseDotSegments`` prefixed the drive path with ``/`` and the
  containment check then compared ``/C:/project/notes.md`` against the
  unmodified ``C:/project/`` root. The equivalent POSIX workdir control passes.

Every case drives the REAL symbols extracted from ``static/workspace.js``
(shared harness of the fourth round: the whole #5747 mutation/refresh block is
taken verbatim). The only seam is ``openFile``, as in the previous rounds.
"""

import json

from tests.test_issue5747_regate4_quoted_and_successor import (
    PRELUDE,
    STATE,
    _run,
)

POSIX = STATE
DRIVE = STATE.replace("workspace:'/w'", "workspace:'C:/project'")
DRIVE_LOWER = STATE.replace("workspace:'/w'", "workspace:'c:/project'")


def _driver(state, body):
    return state + "\n" + PRELUDE + "\n" + body


def _tokens(state, cases, workdir):
    body = (
        "const cases=__PAYLOAD__;\n"
        "const out={};\n"
        "for(const k of Object.keys(cases)) out[k]=_textPathTokens(cases[k],"
        + json.dumps(workdir)
        + ");\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    return json.loads(_run(_driver(state, body), cases))


def _composed(state, command, workdir, preview, extra=""):
    tc = {
        "tid": "t-e6",
        "name": "terminal",
        "arguments": {"command": command, "workdir": workdir},
        "result": "",
    }
    body = (
        "noteWorkspaceMutationsFromToolCall(" + json.dumps(tc) + ");\n"
        "const recorded=[..._turnMutatedPreviewPaths.values()].map(e=>e.path).sort();\n"
        "_previewCurrentPath=" + json.dumps(preview) + ";\n"
        "const started=refreshOpenPreviewIfMutated();\n"
        "(async()=>{ await __pump(true); await started;\n"
        + extra
        + "  process.stdout.write(JSON.stringify({recorded:recorded, calls:__calls,\n"
        "    painted:__painted, paths:[..._turnMutatedPreviewPaths.values()].map(e=>e.path).sort()}));\n"
        "})();\n"
    )
    return json.loads(_run(_driver(state, body)))


def test_write_after_cd_resolves_against_the_effective_directory():
    """E6a: a resolvable ``cd`` moves the directory later writes resolve in."""
    cases = {
        "cd_sed": "cd docs && sed -i 's/a/b/' notes.md",
        "cd_redirect": "cd docs && echo x > notes.md",
        "two_cds": "cd docs && cd sub && sed -i 's/a/b/' notes.md",
        "cd_parent": "cd docs && sed -i 's/a/b/' ../x.md",
        "cd_absolute": "cd /w/docs && sed -i 's/a/b/' notes.md",
        "cd_semicolon": "cd docs; sed -i 's/a/b/' notes.md",
        # Controls: unchanged behavior without a directory change.
        "no_cd": "sed -i 's/a/b/' notes.md",
        "no_cd_read_only": "cd docs && grep -n a notes.md",
        "quoted_cd_in_script": "sed -i 's/cd docs/XX/' notes.md",
        # Negatives: escaping and out-of-workspace directories stay dropped.
        "cd_escapes": "cd docs && sed -i 's/a/b/' ../../out.md",
        "cd_outside": "cd /tmp && sed -i 's/a/b/' notes.md",
    }
    out = _tokens(POSIX, cases, "/w")
    assert out["cd_sed"] == ["docs/notes.md"], (
        "a write after `cd docs` belongs to docs/, not to the workspace root; "
        f"got {out['cd_sed']}"
    )
    assert out["cd_redirect"] == ["docs/notes.md"], (
        f"the same effective directory applies to redirects; got {out['cd_redirect']}"
    )
    assert out["two_cds"] == ["docs/sub/notes.md"], (
        f"chained directory changes accumulate; got {out['two_cds']}"
    )
    assert out["cd_parent"] == ["x.md"], (
        f"`..` after a cd is resolved from the effective directory; got {out['cd_parent']}"
    )
    assert out["cd_absolute"] == ["docs/notes.md"], (
        f"an absolute cd inside the workspace still resolves inside it; got {out['cd_absolute']}"
    )
    assert out["cd_semicolon"] == ["docs/notes.md"], (
        f"a semicolon-separated cd applies to the next command; got {out['cd_semicolon']}"
    )
    assert out["no_cd"] == ["notes.md"], f"the plain form must keep working; got {out['no_cd']}"
    assert out["no_cd_read_only"] == [], (
        f"a read-only command stays a non-mutation; got {out['no_cd_read_only']}"
    )
    assert out["quoted_cd_in_script"] == ["notes.md"], (
        "`cd` inside a quoted sed script is script text, not a directory change; "
        f"got {out['quoted_cd_in_script']}"
    )
    assert out["cd_escapes"] == [], (
        f"a target escaping the workspace is still dropped; got {out['cd_escapes']}"
    )
    assert out["cd_outside"] == [], (
        f"a cd outside the workspace keeps its targets foreign; got {out['cd_outside']}"
    )


def test_drive_letter_workspace_resolves_ordinary_relative_mutations():
    """E6b: drive roots/separators are normalized before the containment check."""
    cases = {
        "plain": "sed -i 's/a/b/' notes.md",
        "subdir": "sed -i 's/a/b/' docs/notes.md",
        "backslash": "sed -i 's/a/b/' docs\\notes.md",
        "after_cd": "cd docs && sed -i 's/a/b/' notes.md",
        "redirect": "echo x > notes.md",
        # Negatives.
        "dotdot": "sed -i 's/a/b/' ../outside.md",
        "foreign_drive": "sed -i 's/a/b/' D:/other/x.md",
        "read_only": "grep -n a notes.md",
    }
    out = _tokens(DRIVE, cases, "C:/project")
    assert out["plain"] == ["notes.md"], (
        "a drive-letter workspace must record an ordinary relative edit; "
        f"got {out['plain']}"
    )
    assert out["subdir"] == ["docs/notes.md"], (
        f"a nested relative target keeps its workspace-relative form; got {out['subdir']}"
    )
    assert out["backslash"] == ["docs/notes.md"], (
        f"Windows separators normalize to the workspace-relative form; got {out['backslash']}"
    )
    assert out["after_cd"] == ["docs/notes.md"], (
        f"the effective directory composes with a drive root; got {out['after_cd']}"
    )
    assert out["redirect"] == ["notes.md"], (
        f"redirects resolve the same way under a drive root; got {out['redirect']}"
    )
    assert out["dotdot"] == [], f"escaping targets stay dropped; got {out['dotdot']}"
    assert out["foreign_drive"] == [], (
        f"a target on another drive is foreign; got {out['foreign_drive']}"
    )
    assert out["read_only"] == [], f"a read-only command is not a mutation; got {out['read_only']}"

    # The drive letter itself is case-insensitive on Windows; the rest of the
    # path keeps its case (POSIX semantics are unaffected).
    lower = _tokens(DRIVE_LOWER, {"plain": "sed -i 's/a/b/' notes.md"}, "C:/project")
    assert lower["plain"] == ["notes.md"], (
        f"the drive letter must compare case-insensitively; got {lower['plain']}"
    )
    posix = _tokens(POSIX, {"plain": "sed -i 's/a/b/' notes.md"}, "/w")
    assert posix["plain"] == ["notes.md"], (
        f"the POSIX control must be unchanged; got {posix['plain']}"
    )


def test_effective_directory_reaches_the_pending_refresh_end_to_end():
    """E6a composed: real terminal payload -> pending map -> drain -> reload."""
    target = "docs/notes.md"
    out = _composed(POSIX, "cd docs && sed -i 's/a/b/' notes.md", "/w", target)
    assert out["recorded"] == [target], (
        f"the real payload must record the effective directory; got {out}"
    )
    assert out["calls"] == [target] and out["painted"] == 1, (
        f"the open preview of that path must reload exactly once; got {out}"
    )
    assert out["paths"] == [], f"the drained entry must be consumed; got {out}"


def test_drive_workspace_reaches_the_pending_refresh_end_to_end():
    """E6b composed: a drive-letter session reloads the open preview too."""
    out = _composed(DRIVE, "sed -i 's/a/b/' notes.md", "C:/project", "notes.md")
    assert out["recorded"] == ["notes.md"], (
        f"the real payload must record the mutation under a drive root; got {out}"
    )
    assert out["calls"] == ["notes.md"] and out["painted"] == 1, (
        f"the open preview must reload exactly once; got {out}"
    )
    assert out["paths"] == [], f"the drained entry must be consumed; got {out}"


def test_unrelated_owner_never_sees_the_mutation():
    """Owner isolation: the recorded obligation is keyed by owner, not by path."""
    body = (
        "noteWorkspaceMutationsFromToolCall("
        + json.dumps(
            {
                "tid": "t-own",
                "name": "terminal",
                "arguments": {"command": "sed -i 's/a/b/' notes.md", "workdir": "/w"},
                "result": "",
            }
        )
        + ");\n"
        "const mine=_turnMutatedPreviewPaths.has(_mutationKey(_currentMutationOwner(),'notes.md'));\n"
        "S.activeProfile='other';\n"
        "const foreign=_turnMutatedPreviewPaths.has(_mutationKey(_currentMutationOwner(),'notes.md'));\n"
        "process.stdout.write(JSON.stringify({mine:mine, foreign:foreign}));\n"
    )
    out = json.loads(_run(_driver(POSIX, body)))
    assert out["mine"] is True, f"the writing owner must see its own mutation; got {out}"
    assert out["foreign"] is False, f"another profile must not see it; got {out}"
