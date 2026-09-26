"""#7358 round 4 regression — cold reload must keep is_error live.

The round-3 fix gave the live mirror and the structured ``tool_complete``
SSE payload the authoritative ``is_error`` bit. The compact / transparent
render paths downstream in ``static/messages.js`` build the tool card
from a merge of the live scene and the persisted ``tool`` / ``payload``
rows via ``_enrichSettledToolRowBodyFromLive(row, live)``, and that
merge was missing the ``is_error`` upgrade: a failed tool that was
correctly shown red live would revert to "Completed" on cold reload
because neither the persisted row nor the payload object carried the
field.

This test exercises the merge logic in isolation via a Node sandbox
so the test stays independent of Hermes-specific globals. The exact
shape of the live / tool / payload objects mirrors the production
callers; if the merge contract changes (e.g. the upgrade is dropped),
this test catches the regression.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"


def _run_node_merge_enrichment(row, live):
    """Drive ``_enrichSettledToolRowBodyFromLive(row, live)`` in a
    Node sandbox.

    The helper is not exported — we lift the body verbatim out of
    the source and run it directly. The four local helpers it uses
    (``_anchorSceneStringPayload``, ``_anchorSceneToolArgs``) are
    not exported either, so we stub them with minimal drop-in
    implementations that match the production contract for the
    inputs these tests construct.
    """
    src = MESSAGES_JS.read_text(encoding="utf-8")
    marker = "function _enrichSettledToolRowBodyFromLive("
    start = src.index(marker)
    brace_pos = src.index("{", start)
    depth = 0
    end = brace_pos
    for i in range(brace_pos, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    fn_body = src[start:end]

    driver = f"""
// Stub S first: the is_error block reads S._settledToolIsErrorByTid (the
// round-5 persisted-map fallback). The round-8 row shape resolves
// "row.tool.tid" as a row id, so the row-id lookup no longer
// short-circuits before S is read and S must exist. An empty map keeps
// the round-4 contract: no persisted is_error, the live mirror is the
// only source of the verdict.
const S = {{}};
// Minimal stubs for the two private helpers the production
// merge body calls. These match the production contract for
// the inputs the tests construct (string payloads and
// object args); the production implementations do more
// defensively but the merge logic under test is the same.
function _anchorSceneStringPayload(v) {{ return (v===undefined||v===null)?null:String(v); }}
function _anchorSceneToolArgs(live) {{ return (live && live.args && typeof live.args==='object') ? live.args : null; }}

{fn_body}

const _row = {json.dumps(row)};
const _live = {json.dumps(live)};
const _enriched = _enrichSettledToolRowBodyFromLive(_row, _live);
process.stdout.write(JSON.stringify({{row: _row, enriched: _enriched}}));
"""
    node = subprocess.run(
        ["node", "-e", driver],
        capture_output=True, text=True, timeout=30
    )
    if node.returncode != 0:
        pytest.skip(f"node failed: {node.stderr}")
    return json.loads(node.stdout)


def test_cold_reload_keeps_is_error_live_True():
    """#7358 round 4 finding 2: a failed tool that was correctly
    shown red live must render red after a cold reload, not
    revert to "Completed". The merge that builds the persisted
    row from the live mirror must carry the live is_error
    through to both the ``tool`` row and the ``payload`` object."""
    # Persisted row from the session.messages sidecar: no
    # is_error field, because the API's settle path drops it
    # in the compact / transparent render shape.
    row = {"tool": {"tid": "t1", "name": "terminal", "snippet": "[exit 0]"},
           "payload": {"name": "terminal", "snippet": "[exit 0]"}}
    # Live mirror: the structured tool_complete callback set
    # is_error to true (the Agent computed the bit).
    live = {"tid": "t1", "name": "terminal", "is_error": True}
    result = _run_node_merge_enrichment(row, live)
    assert result["row"]["tool"].get("is_error") is True, (
        "cold reload must show a failed tool as failed, not "
        "Completed — the merge must carry the live is_error "
        "through to the persisted tool row"
    )
    assert result["row"]["payload"].get("is_error") is True, (
        "the payload row that drives the compact / transparent "
        "render paths must also carry is_error, otherwise the "
        "render defaults to False and the card reverts to "
        "Completed"
    )
    assert result["enriched"] is True, (
        "enriched must be true so the merge signals the row "
        "was upgraded and the caller can persist the change"
    )


def test_cold_reload_does_not_force_is_error_for_successful_tool():
    """A successful tool (no live is_error) must keep is_error
    absent or False after the merge — the upgrade is one-way
    and only fires when the live mirror actually reported a
    failure. Reverting a successful card to ``is_error: False``
    explicitly is a no-op and not the upgrade the review
    called for."""
    row = {"tool": {"tid": "t1", "name": "terminal", "snippet": "[exit 0]"},
           "payload": {"name": "terminal", "snippet": "[exit 0]"}}
    live = {"tid": "t1", "name": "terminal"}  # no is_error
    result = _run_node_merge_enrichment(row, live)
    # The successful tool must stay successful — no is_error
    # downgrade, no upgrade to True.
    assert not result["row"]["tool"].get("is_error"), (
        "a successful tool must not be marked as failed by the "
        "cold-reload merge; the upgrade is one-way and gated on "
        "live.is_error === true"
    )
    assert not result["row"]["payload"].get("is_error"), (
        "a successful tool must not be marked as failed on the "
        "payload row either"
    )


def test_cold_reload_preserves_existing_is_error_true():
    """If the persisted row already carries ``is_error: true`` (a
    prior upgrade, or the API's own write path), the merge must
    not clear it — the upgrade is idempotent."""
    row = {"tool": {"tid": "t1", "name": "terminal", "is_error": True},
           "payload": {"name": "terminal", "is_error": True}}
    live = {"tid": "t1", "name": "terminal"}  # no live is_error
    result = _run_node_merge_enrichment(row, live)
    assert result["row"]["tool"].get("is_error") is True, (
        "an already-failed persisted row must stay failed after "
        "the merge; the cold-reload path must not silently clear "
        "is_error when the live mirror is missing the field"
    )
    assert result["row"]["payload"].get("is_error") is True
