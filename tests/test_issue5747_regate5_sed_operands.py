"""#5747 fifth round (review 2026-11-25T14:29:02Z): ``sed`` file operands.

The maintainer reproduced two remaining target-parsing defects on ``5f7ea22eb``:

* Q5a - a quoted file operand with spaces is split on whitespace, so only its
  last fragment survives: ``sed -i 's/a/b/' 'docs/release notes.md'`` publishes a
  mutation for ``notes.md`` instead of ``docs/release notes.md``.
* Q5b - only the final token of the invocation is collected, so a multi-operand
  command loses every operand but the last: ``sed -i 's/a/b/' one.css two.css``
  publishes only ``two.css``.

Both live in ``_textWriteOpTargets`` (``static/workspace.js``): the
command-boundary scan respects quotes, but the segment is then split on all
whitespace and only ``toks[toks.length-1]`` is pushed. The repair has to keep
quoting/escaping while tokenizing the supported sed arguments and collect
**every** file operand, without touching the containment/read-only negatives.

Every case drives the REAL symbols extracted from ``static/workspace.js``
(shared with the fourth-round module: the whole #5747 mutation/refresh block is
taken verbatim, so a tokenizer helper the repair adds inside that block is
exercised too). The only seam is ``openFile``, exactly as in the previous round.
"""

import json
import shutil

import pytest

from tests.test_issue5747_regate4_quoted_and_successor import (
    _driver,
    _paths_expr,
    _run,
)

NODE = shutil.which("node") or ""

pytestmark = pytest.mark.skipif(not NODE, reason="node not on PATH")


def test_every_sed_file_operand_survives_tokenization():
    """Q5a + Q5b: quoting is preserved and every file operand is collected."""
    payload = {
        "quoted_space": "sed -i 's/a/b/' 'docs/release notes.md'",
        "double_quoted_space": 'sed -i "s/a/b/" "docs/my file.md"',
        "multi_operand": "sed -i 's/a/b/' one.css two.css",
        "quoted_space_multi": "sed -i 's/a/b/' 'docs/release notes.md' src/app.js",
        "script_not_a_file": "sed -i 's/a/b/'",
    }
    driver = _driver(
        "const cases=__PAYLOAD__;\n"
        "const out={};\n"
        "for(const k of Object.keys(cases)) out[k]=_textWriteOpTargets(cases[k]);\n"
        "out.quoted_space_resolved=_textPathTokens(cases.quoted_space,'/w');\n"
        "out.multi_resolved=_textPathTokens(cases.multi_operand,'/w');\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    out = json.loads(_run(driver, payload))
    assert out["quoted_space"] == ["docs/release notes.md"], (
        "a quoted filename with spaces is one operand, not its last word; "
        f"got {out}"
    )
    assert out["double_quoted_space"] == ["docs/my file.md"], (
        f"double-quoted operands must dequote the same way; got {out}"
    )
    assert out["multi_operand"] == ["one.css", "two.css"], (
        f"every file operand of the invocation must be collected; got {out}"
    )
    assert out["quoted_space_multi"] == ["docs/release notes.md", "src/app.js"], (
        f"quoting and multiple operands must compose; got {out}"
    )
    assert out["quoted_space_resolved"] == ["docs/release notes.md"], (
        f"the quoted operand must resolve against the tool workdir; got {out}"
    )
    assert out["multi_resolved"] == ["one.css", "two.css"], (
        f"both operands must resolve against the tool workdir; got {out}"
    )
    assert out["script_not_a_file"] == [], (
        f"with no file operand the sed script is not a target; got {out}"
    )


def test_sed_operand_controls_stay_negative_or_unchanged():
    """Controls: read-only forms, script arguments and containment negatives.

    These pass before the repair as well - they constrain it, so a tokenizer
    that publishes the sed script (or an ``-f`` script file, or a read-only
    mention) is rejected.
    """
    payload = {
        "script_with_space": "sed -i 's/a b/c d/' file.css",
        "dash_e": "sed -i -e 's/a/b/' one.css",
        "dash_f": "sed -i -f fix.sed one.css",
        "read_only_sed": "sed -n 'p' 'docs/notes.md'",
        "read_only_grep": "grep -n 'a b' 'docs/release notes.md'",
        "quoted_amp": "sed -i 's/red/blue & green/' static/style.css",
        "two_commands": "sed -i 's/a/b/' one.css && sed -i 's/c/d/' two.css",
        "escapes": "sed -i 's/a/b/' '../outside.css'",
    }
    driver = _driver(
        "const cases=__PAYLOAD__;\n"
        "const out={};\n"
        "for(const k of Object.keys(cases)) out[k]=_textWriteOpTargets(cases[k]);\n"
        "out.escaped_paths=_textPathTokens(cases.escapes,'/w');\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    out = json.loads(_run(driver, payload))
    assert out["script_with_space"] == ["file.css"], (
        f"a quoted script with spaces is not a file operand; got {out}"
    )
    assert out["dash_e"] == ["one.css"], (
        f"an explicit -e script must not be published as a target; got {out}"
    )
    assert out["dash_f"] == ["one.css"], (
        f"an -f script file is not a mutation target; got {out}"
    )
    assert out["read_only_sed"] == [], (
        f"sed without -i only reads; it is not a mutation; got {out}"
    )
    assert out["read_only_grep"] == [], (
        f"a read-only grep with a quoted space is not a mutation; got {out}"
    )
    assert out["quoted_amp"] == ["static/style.css"], (
        f"the quoted-& repair must keep working; got {out}"
    )
    assert out["two_commands"] == ["one.css", "two.css"], (
        f"both chained invocations must be found; got {out}"
    )
    assert out["escaped_paths"] == [], (
        f"a target outside the workspace must still be dropped; got {out}"
    )


def test_quoted_operand_reaches_the_pending_refresh_end_to_end():
    """Q5a composed: real terminal payload -> mutation map -> drain -> reload."""
    target = "docs/release notes.md"
    tc = {
        "tid": "t1",
        "name": "terminal",
        "arguments": {"command": f"sed -i 's/a/b/' '{target}'", "workdir": "/w"},
        "result": "",
    }
    driver = _driver(
        "noteWorkspaceMutationsFromToolCall(" + json.dumps(tc) + ");\n"
        "const recorded=" + _paths_expr() + ";\n"
        f"_previewCurrentPath={json.dumps(target)};\n"
        "const started=refreshOpenPreviewIfMutated();\n"
        "(async()=>{ await __pump(true); await started;\n"
        "  process.stdout.write(JSON.stringify({recorded:recorded, calls:__calls,\n"
        "    painted:__painted, paths:" + _paths_expr() + "}));\n"
        "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["recorded"] == [target], (
        f"the real payload must record the quoted operand; got {out}"
    )
    assert out["calls"] == [target] and out["painted"] == 1, (
        f"the open preview must be reloaded exactly once; got {out}"
    )
    assert out["paths"] == [], f"the drained entry must be consumed; got {out}"


def test_multi_operand_reaches_the_pending_refresh_end_to_end():
    """Q5b composed: a two-operand sed records both paths, reloads only the open one."""
    tc = {
        "tid": "t2",
        "name": "terminal",
        "arguments": {"command": "sed -i 's/a/b/' one.css two.css", "workdir": "/w"},
        "result": "",
    }
    driver = _driver(
        "noteWorkspaceMutationsFromToolCall(" + json.dumps(tc) + ");\n"
        "const recorded=" + _paths_expr() + ";\n"
        "_previewCurrentPath='two.css';\n"
        "const started=refreshOpenPreviewIfMutated();\n"
        "(async()=>{ await __pump(true); await started;\n"
        "  process.stdout.write(JSON.stringify({recorded:recorded, calls:__calls,\n"
        "    painted:__painted, paths:" + _paths_expr() + "}));\n"
        "})();\n"
    )
    out = json.loads(_run(driver))
    assert out["recorded"] == ["one.css", "two.css"], (
        f"both operands must be recorded as pending mutations; got {out}"
    )
    assert out["calls"] == ["two.css"] and out["painted"] == 1, (
        f"only the open preview is reloaded; got {out}"
    )
    assert out["paths"] == ["one.css"], (
        f"the unopened operand keeps its obligation for a later open; got {out}"
    )
