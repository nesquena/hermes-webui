"""#5747 seventh round (review 2026-11-26T04:23:49Z at ``c9496e0913a0``): three
parser regressions introduced by the tokenizer/segmenter repair.

* P7a - multiline Python writes are not tracked. ``execute_code`` code that
  calls ``open('report.md',`` / ``'w',`` / ``encoding='utf-8').write(...)`` on
  several lines records no mutation and issues no refresh, because the shell
  segmenter splits at every unquoted newline before the ``open(...)`` regex can
  see the complete call.
* P7b - a pipeline's (or background job's) ``cd`` wrongly moves the inferred
  parent directory. ``cd docs | cat; sed -i 's/a/b/' notes.md`` edits the root
  ``notes.md``, but the parser records ``docs/notes.md``; the open root file
  therefore receives no refresh.
* P7c - POSIX escaped filename characters become path separators.
  ``sed -i 's/a/b/' docs/a\\&b.md`` edits ``docs/a&b.md``, but the tokenizer
  substitutes ``/`` for the escape and records ``docs/a/&b.md``.

Every case drives the REAL symbols extracted from ``static/workspace.js`` (the
shared harness of the fourth round takes the whole #5747 mutation/refresh block
verbatim). The only seam is ``openFile``, as in the previous rounds. The
controls keep the repaired directory, drive-root, quoting, containment,
ownership, dirty-pane and read-only contracts intact.
"""

import json

from tests.test_issue5747_regate4_quoted_and_successor import (
    PRELUDE,
    STATE,
    _run,
)

POSIX = STATE

MULTILINE_PY = "open('report.md',\n 'w',\n encoding='utf-8').write('updated')"


def _driver(body):
    return STATE + "\n" + PRELUDE + "\n" + body


def _paths_expr():
    return "[..._turnMutatedPreviewPaths.values()].map(e=>e.path).sort()"


def _tokens(cases, workdir="/w"):
    body = (
        "const cases=__PAYLOAD__;\n"
        "const out={};\n"
        "for(const k of Object.keys(cases)) out[k]=_textPathTokens(cases[k],"
        + json.dumps(workdir)
        + ");\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    return json.loads(_run(_driver(body), cases))


def _composed(name, args, result, preview):
    tc = {"tid": "t-p7", "name": name, "arguments": args, "result": result}
    body = (
        "noteWorkspaceMutationsFromToolCall(" + json.dumps(tc) + ");\n"
        "const recorded=" + _paths_expr() + ";\n"
        "_previewCurrentPath=" + json.dumps(preview) + ";\n"
        "const started=refreshOpenPreviewIfMutated();\n"
        "(async()=>{ await __pump(true); await started;\n"
        "  process.stdout.write(JSON.stringify({recorded:recorded, calls:__calls,\n"
        "    painted:__painted, paths:" + _paths_expr() + "}));\n"
        "})();\n"
    )
    return json.loads(_run(_driver(body)))


def test_multiline_python_write_is_tracked():
    """P7a: a Python write split across lines is still one write op."""
    cases = {
        "multiline": MULTILINE_PY,
        "multiline_two_lines": "open('report.md',\n    'w').write('updated')",
        "multiline_append": "open('out.log',\n    'a').write('line')",
        # Controls: the single-line forms and the read-only negative.
        "single_line": "open('report.md', 'w').write('x')",
        "single_line_append": "open('report.md', 'a').write('x')",
        "read_only": "open('report.md', 'r').read()",
        "read_only_multiline": "open('report.md',\n    'r').read()",
        "write_file_multiline": "write_file('made.md',\n  'body')",
    }
    out = _tokens(cases)
    assert out["multiline"] == ["report.md"], (
        "a write op spread over several lines must still be recorded; "
        f"got {out}"
    )
    assert out["multiline_two_lines"] == ["report.md"], (
        f"the shorter multiline form must be recorded too; got {out}"
    )
    assert out["multiline_append"] == ["out.log"], (
        f"the append mode counts as a write in any layout; got {out}"
    )
    assert out["single_line"] == ["report.md"], (
        f"the single-line form must keep working; got {out}"
    )
    assert out["single_line_append"] == ["report.md"], (
        f"the single-line append must keep working; got {out}"
    )
    assert out["read_only"] == [], f"a read is not a mutation; got {out}"
    assert out["read_only_multiline"] == [], (
        f"a multiline read is not a mutation either; got {out}"
    )
    assert out["write_file_multiline"] == ["made.md"], (
        f"the write_file call keeps its target; got {out}"
    )


def test_multiline_python_write_reaches_the_pending_refresh():
    """P7a composed: execute_code payload -> pending map -> drain -> reload."""
    out = _composed(
        "execute_code",
        {"code": MULTILINE_PY, "workdir": "/w"},
        "",
        "report.md",
    )
    assert out["recorded"] == ["report.md"], (
        f"the real execute_code payload must record the multiline write; got {out}"
    )
    assert out["calls"] == ["report.md"] and out["painted"] == 1, (
        f"the open preview must reload exactly once; got {out}"
    )
    assert out["paths"] == [], f"the drained entry must be consumed; got {out}"


def test_pipeline_and_background_cd_do_not_move_the_parent_directory():
    """P7b: a subshell ``cd`` never changes the directory of later commands."""
    cases = {
        "pipe": "cd docs | cat; sed -i 's/a/b/' notes.md",
        "pipe_alone": "cd docs | cat",
        "background": "cd docs & sleep 1; sed -i 's/a/b/' notes.md",
        # Controls: the parent-shell separators keep moving the directory.
        "semicolon": "cd docs; sed -i 's/a/b/' notes.md",
        "andand": "cd docs && sed -i 's/a/b/' notes.md",
        "background_job_then_cd": "sleep 1 & cd docs; sed -i 's/a/b/' notes.md",
        "andand_then_pipeline": "cd docs && cat | sed -i 's/a/b/' sub/notes.md",
        "no_cd": "sed -i 's/a/b/' notes.md",
        "read_only": "cd docs | grep -n a notes.md",
    }
    out = _tokens(cases)
    assert out["pipe"] == ["notes.md"], (
        "the cd of a pipeline runs in a subshell, so the later sed edits the "
        f"root file; got {out}"
    )
    assert out["pipe_alone"] == [], f"a bare pipeline is not a mutation; got {out}"
    assert out["background"] == ["notes.md"], (
        "a background cd must not move the parent directory either; "
        f"got {out}"
    )
    assert out["semicolon"] == ["docs/notes.md"], (
        f"a semicolon-separated cd still applies; got {out}"
    )
    assert out["andand"] == ["docs/notes.md"], (
        f"an &&-chained cd still applies; got {out}"
    )
    assert out["background_job_then_cd"] == ["docs/notes.md"], (
        "a background job followed by a real cd keeps the effective directory; "
        f"got {out}"
    )
    assert out["andand_then_pipeline"] == ["docs/sub/notes.md"], (
        "the &&-chained directory composes with a piped write; "
        f"got {out}"
    )
    assert out["no_cd"] == ["notes.md"], f"the plain form must keep working; got {out}"
    assert out["read_only"] == [], f"a read-only pipeline is not a mutation; got {out}"


def test_pipeline_cd_reaches_the_pending_refresh_of_the_root_file():
    """P7b composed: the real payload refreshes the file the command edited."""
    out = _composed(
        "terminal",
        {"command": "cd docs | cat; sed -i 's/a/b/' notes.md", "workdir": "/w"},
        "",
        "notes.md",
    )
    assert out["recorded"] == ["notes.md"], (
        f"the root file is the one the pipeline wrote; got {out}"
    )
    assert out["calls"] == ["notes.md"] and out["painted"] == 1, (
        f"the open root preview must reload exactly once; got {out}"
    )
    assert out["paths"] == [], f"the drained entry must be consumed; got {out}"


def test_posix_escaped_operands_keep_their_literal_characters():
    """P7c: a backslash escape is an argument character, not a separator."""
    cases = {
        "amp": "sed -i 's/a/b/' docs/a\\&b.md",
        "paren": "sed -i 's/a/b/' docs/a\\(b\\).md",
        "dollar": "sed -i 's/a/b/' docs/a\\$b.md",
        "semicolon": "sed -i 's/a/b/' docs/a\\;b.md",
        # Controls: quoted names, Windows separators and containment.
        "quoted_space": "sed -i 's/a/b/' 'docs/a b.md'",
        "windows_separator": "sed -i 's/a/b/' docs\\\\notes.md",
        "quoted_separator": "sed -i 's/red/blue & green/' static/style.css",
        "escapes_workspace": "sed -i 's/a/b/' ../../a\\&b.md",
        "read_only": "grep -n a docs/a\\&b.md",
    }
    out = _tokens(cases)
    assert out["amp"] == ["docs/a&b.md"], (
        f"the escaped ampersand belongs to the filename; got {out}"
    )
    assert out["paren"] == ["docs/a(b).md"], (
        f"escaped parentheses belong to the filename; got {out}"
    )
    assert out["dollar"] == ["docs/a$b.md"], (
        f"an escaped dollar sign stays literal in the name; got {out}"
    )
    assert out["semicolon"] == ["docs/a;b.md"], (
        f"an escaped semicolon stays literal in the name; got {out}"
    )
    assert out["quoted_space"] == ["docs/a b.md"], (
        f"a quoted name with spaces must keep working; got {out}"
    )
    assert out["windows_separator"] == ["docs/notes.md"], (
        f"a Windows separator still normalizes; got {out}"
    )
    assert out["quoted_separator"] == ["static/style.css"], (
        f"the quoted-separator repair must stay intact; got {out}"
    )
    assert out["escapes_workspace"] == [], (
        f"an escaping target is still dropped; got {out}"
    )
    assert out["read_only"] == [], f"a read-only command is not a mutation; got {out}"


def test_escaped_operand_reaches_the_pending_refresh():
    """P7c composed: the open preview of ``docs/a&b.md`` is refreshed."""
    out = _composed(
        "terminal",
        {"command": "sed -i 's/a/b/' docs/a\\&b.md", "workdir": "/w"},
        "",
        "docs/a&b.md",
    )
    assert out["recorded"] == ["docs/a&b.md"], (
        f"the escaped name must be recorded verbatim; got {out}"
    )
    assert out["calls"] == ["docs/a&b.md"] and out["painted"] == 1, (
        f"the open preview of that name must reload exactly once; got {out}"
    )
    assert out["paths"] == [], f"the drained entry must be consumed; got {out}"


def test_python_and_write_file_targets_follow_the_effective_directory():
    """P7d: the python pass must not lose the effective directory.

    A python call embedded in a command text runs with the shell's current
    directory, so ``cd docs && python3 -c "open('a.txt','w')"`` writes
    ``docs/a.txt``. Parsing the calls across the whole text is only allowed if
    the directory in force at the call site is kept.
    """
    cases = {
        "py_after_cd": "cd docs && python3 -c \"open('a.txt','w')\"",
        "py_after_semicolon": "cd docs; open('a.txt','w')",
        "wf_after_cd": "cd docs && write_file('made.txt', 'b')",
        "py_multiline_after_cd": "cd docs && python3 -c \"open('a.txt',\n 'w')\"",
        "py_multiline_after_cd2": "cd docs && open('a.txt',\n 'w').write('x')",
        "pipe_cd_then_sub": "cd docs | cat; cd sub; sed -i 's/a/b/' x.txt",
        # Controls.
        "py_no_cd": "open('a.txt', 'w')",
        "py_read_only_after_cd": "cd docs && python3 -c \"open('a.txt')\"",
    }
    out = _tokens(cases)
    assert out["py_after_cd"] == ["docs/a.txt"], (
        f"the call runs with the effective directory, so it writes docs/a.txt; got {out}"
    )
    assert out["py_after_semicolon"] == ["docs/a.txt"], (
        f"a semicolon-separated cd applies to the call too; got {out}"
    )
    assert out["wf_after_cd"] == ["docs/made.txt"], (
        f"the write_file form resolves the same way; got {out}"
    )
    assert out["py_multiline_after_cd"] == ["docs/a.txt"], (
        f"a quoted multiline call keeps the effective directory; got {out}"
    )
    assert out["py_multiline_after_cd2"] == ["docs/a.txt"], (
        f"the unquoted multiline call keeps the effective directory; got {out}"
    )
    assert out["pipe_cd_then_sub"] == ["sub/x.txt"], (
        "the subshell cd is skipped, so the real cd lands on sub/; "
        f"got {out}"
    )
    assert out["py_no_cd"] == ["a.txt"], f"the plain form is unchanged; got {out}"
    assert out["py_read_only_after_cd"] == [], (
        f"a read-only call stays a non-mutation; got {out}"
    )


def test_owner_isolation_and_dirty_pane_survive_the_repair():
    """Control: ownership and the dirty fence are untouched by this round."""
    body = (
        "noteWorkspaceMutationsFromToolCall("
        + json.dumps(
            {
                "tid": "t-own",
                "name": "execute_code",
                "arguments": {"code": MULTILINE_PY, "workdir": "/w"},
                "result": "",
            }
        )
        + ");\n"
        "const mine=_turnMutatedPreviewPaths.has("
        "_mutationKey(_currentMutationOwner(),'report.md'));\n"
        "S.activeProfile='other';\n"
        "const foreign=_turnMutatedPreviewPaths.has("
        "_mutationKey(_currentMutationOwner(),'report.md'));\n"
        "S.activeProfile='default';\n"
        "_previewCurrentPath='report.md';\n"
        "_edit('unsaved');\n"
        "const started=refreshOpenPreviewIfMutated();\n"
        "(async()=>{ await __pump(true); await started;\n"
        "  process.stdout.write(JSON.stringify({mine:mine, foreign:foreign,\n"
        "    calls:__calls, painted:__painted, unsaved:_previewHasUnsavedEdits()}));\n"
        "})();\n"
    )
    out = json.loads(_run(_driver(body)))
    assert out["mine"] is True, f"the writing owner must see its mutation; got {out}"
    assert out["foreign"] is False, f"another profile must not see it; got {out}"
    assert out["calls"] == [] and out["painted"] == 0, (
        f"a dirty pane is never reloaded by the drain; got {out}"
    )
    assert out["unsaved"] is True, f"the unsaved edit must survive; got {out}"
