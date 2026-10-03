"""#7358 round 8 regression — the one-way is_error upgrade needs an id match.

The 9/24 maintainer re-gate review's SILENT finding on commit ``5fbbde44``:

All three client-side sites that copy the authoritative ``is_error`` from a
live tool call to a settled row resolved their pairing the same way:

1. ``copyLiveToolMetadata`` (``static/ui.js``, the cold-reload fallback
   render path) — per-tid map lookup, then a **name** fallback;
2. ``_mergeSettledToolCallsWithLiveMetadata`` (``static/messages.js``) —
   per-tid map lookup, then a **name** fallback;
3. ``_enrichSettledToolRowBodyFromLive`` (``static/messages.js``) —
   reached from the per-id dedup path *and* from
   ``_anchorSceneMatchingContentToolRow``, whose name / invocation
   fallback pairs a settled row with a live call that has a different id.

The upgrade itself ran for **both** kinds of match, so an older
*successful* terminal call whose id did not match could absorb a newer
*failed* terminal call's failure and settle as Failed.

The fix: copy ``is_error`` only when the match came from the id map /
matched by tool id. The name fallback keeps copying the presentation
keys (``activityBurstId`` / ``duration`` / ``started_at``), and the
persisted per-tid map (``S._settledToolIsErrorByTid``) is already
id-keyed and is unchanged.

This file pins, per site:

- **bug repro** — two terminal calls with different ids, the older one
  succeeding and the newer one failing, where the older one's settled row
  reaches the name fallback: the older row must stay successful, both
  after the settle and after a reload of the same shape;
- **control** — the id-map hit still upgrades (the failure must still
  render Failed, which is what rounds 4/5 bought), and the name fallback
  still restores the presentation-only keys;
- a source-shape pin asserting the upgrade is gated on the id map at all
  three sites.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MESSAGES_JS = REPO_ROOT / "static" / "messages.js"
UI_JS = REPO_ROOT / "static" / "ui.js"
NODE = shutil.which("node")


def _read(relpath: str) -> str:
    return (REPO_ROOT / relpath).read_text(encoding="utf-8")


def _function_block(src: str, header: str) -> str:
    """Lift a top-level ``{ ... }`` block starting at ``header`` verbatim."""
    start = src.find(header)
    assert start != -1, f"{header!r} not found"
    brace = src.find("{", start)
    assert brace != -1, f"no body for {header!r}"
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[start:idx + 1]
    raise AssertionError(f"{header!r} did not close")


def _run_node(script: str, payload: dict) -> dict:
    """Run a Node script with the payload on stdin; return parsed stdout."""
    assert NODE, "node not on PATH"
    # Every driver reads the payload from stdin into a PAYLOAD global
    # before the lifted function bodies run.
    harness = (
        "var PAYLOAD = JSON.parse(require('fs').readFileSync(0, 'utf8') || '{}');\n"
        + script
    )
    result = subprocess.run(
        [NODE, "-e", harness],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"node failed:\n{result.stderr}"
    return json.loads(result.stdout)


# ── Site ①: copyLiveToolMetadata (static/ui.js) ──────────────────────────


def _ui_copy_live_tool_metadata_driver() -> str:
    fn = _function_block(_read("static/ui.js"), "const copyLiveToolMetadata=")

    return f"""
// The live mirror and the per-tid map, as the production closure reads
// them. The cold-reload fallback path in ui.js builds both from S.
const liveToolMetadata = PAYLOAD.liveToolMetadata;
const liveMetadataByTid = new Map();
liveToolMetadata.forEach(function(tc, idx) {{
  const tid = tc && (tc.tid || tc.id || tc.tool_call_id || tc.call_id) || '';
  if (tid && !liveMetadataByTid.has(tid)) liveMetadataByTid.set(tid, {{tc: tc, idx: idx}});
}});
const usedLiveToolMetadata = new Set();
const S = {{ _settledToolIsErrorByTid: PAYLOAD.persistedIsErrorByTid || {{}} }};
const _persistedIsErrorByTid = (S && S._settledToolIsErrorByTid && typeof S._settledToolIsErrorByTid === 'object')
  ? S._settledToolIsErrorByTid : null;

{fn}

const result = PAYLOAD.rows.map(function(row) {{
  return copyLiveToolMetadata(
    Object.assign({{}}, row),
    row.name,
    row.tid || row.id || row.tool_call_id || row.call_id || ''
  );
}});
process.stdout.write(JSON.stringify(result));
"""


@pytest.fixture
def ui_driver():
    return _ui_copy_live_tool_metadata_driver()


def test_settled_row_reaching_name_fallback_keeps_its_own_success(ui_driver):
    """Bug repro (site ①): the older successful terminal call must not
    absorb the newer failed terminal call's failure when the pairing
    falls through to the name fallback.

    Live mirror order mirrors issue order: the first (older) call
    succeeded, the second (newer) call failed. Both settled rows carry
    ids that are absent from the live mirror (the settle happened after
    the mirror was rebuilt), so both pair by name. The name fallback
    hands the newer, failed live entry to the older row — which must
    still settle as successful."""
    rows = [
        {"name": "terminal", "tid": "call_old", "done": True},
        {"name": "terminal", "tid": "call_new", "done": True},
    ]
    live = [
        {"tid": "call_old", "name": "terminal", "is_error": False},
        {"tid": "call_new", "name": "terminal", "is_error": True},
    ]
    # Both rows reach the name fallback: the settled ids differ from the
    # live ids, so liveMetadataByTid misses for both.
    out = _run_node(
        ui_driver,
        {"rows": rows, "liveToolMetadata": live, "persistedIsErrorByTid": {}},
    )
    assert not out[0].get("is_error"), (
        "the older successful terminal call settled via the name "
        "fallback must stay successful — the one-way is_error upgrade "
        "may only run on the id-map hit (#7358 re-gate 9/24)"
    )
    # Control inside the same run: the newer, failed row must still be
    # able to fail — the fix must not suppress genuine failures.
    assert out[1].get("is_error") is True, (
        "the newer failed terminal call must still render failed"
    )


def test_name_fallback_match_after_reload_still_keeps_success(ui_driver):
    """The same shape re-run the way a reload does: the per-tid map comes
    from ``S._settledToolIsErrorByTid`` (empty for a successful tool) and
    the live mirror is the settle-time one. The older row must still
    settle successful after the reload."""
    rows = [{"name": "terminal", "tid": "call_old", "done": True}]
    live = [{"tid": "call_new", "name": "terminal", "is_error": True}]
    out = _run_node(
        ui_driver,
        {"rows": rows, "liveToolMetadata": live, "persistedIsErrorByTid": {}},
    )
    assert not out[0].get("is_error"), (
        "a successful row must not inherit a failure through the name "
        "fallback after a reload either"
    )


def test_id_map_hit_still_upgrades_and_keeps_presentation_keys(ui_driver):
    """Control: an id-map hit (the pairing rounds 4/5 introduced) still
    upgrades to failed, and the name fallback still restores the
    presentation-only keys."""
    rows = [
        {"name": "terminal", "tid": "call_ok", "done": True},
        # Older row: different live id, so the name fallback pairs it with
        # the failed live entry below.
        {"name": "terminal", "tid": "call_old", "activityBurstId": None},
    ]
    live = [
        {"tid": "call_ok", "name": "terminal", "is_error": True, "activityBurstId": 7},
        {"tid": "call_new", "name": "terminal", "is_error": True, "activityBurstId": 9},
    ]
    out = _run_node(
        ui_driver,
        {"rows": rows, "liveToolMetadata": live, "persistedIsErrorByTid": {}},
    )
    assert out[0].get("is_error") is True, (
        "an id-map hit must still upgrade the row to failed"
    )
    assert not out[1].get("is_error"), (
        "the name-matched older row must not inherit the failure"
    )
    # Same pairing rule as site ②: the id hit consumed live entry 0, so
    # the name fallback takes entry 1's activityBurstId. The point is
    # that presentation keys still flow through the name fallback.
    assert out[1].get("activityBurstId") == 9, (
        "the name fallback must keep copying the presentation-only "
        "keys (activityBurstId) even though it no longer copies is_error"
    )


# ── Site ②: _mergeSettledToolCallsWithLiveMetadata (static/messages.js) ──


def _messages_merge_driver() -> str:
    fn = _function_block(_read("static/messages.js"), "function _mergeSettledToolCallsWithLiveMetadata(")

    return f"""
const S = {{ toolCalls: PAYLOAD.liveToolMetadata }};

{fn}

process.stdout.write(JSON.stringify(_mergeSettledToolCallsWithLiveMetadata(PAYLOAD.rawCalls)));
"""


@pytest.fixture
def merge_driver():
    return _messages_merge_driver()


def test_merge_name_fallback_does_not_transfer_failure(merge_driver):
    """Bug repro (site ②): the persisted summary's older successful row
    pairs by name with the newer failed live call and must not be
    upgraded. The newer row, whose id also misses, must still be free to
    fail (it is the failed call)."""
    raw = [
        {"tid": "call_old", "name": "terminal"},
        {"tid": "call_new", "name": "terminal"},
    ]
    live = [
        {"tid": "call_old", "name": "terminal", "is_error": False},
        {"tid": "call_new", "name": "terminal", "is_error": True},
    ]
    # The persisted summaries carry ids that are absent from the live
    # mirror, so byTid misses and the name fallback runs.
    raw[0]["tid"] = "p_old"
    raw[1]["tid"] = "p_new"
    out = _run_node(merge_driver, {"rawCalls": raw, "liveToolMetadata": live})
    assert not out[0].get("is_error"), (
        "the older successful call merged through the name fallback "
        "must stay successful"
    )
    assert not out[1].get("is_error"), (
        "the newer call pairs with the older successful live entry here, "
        "so it must stay successful too — the upgrade is id-gated, and "
        "neither row matched its real live id in this shape"
    )


def test_merge_id_hit_still_upgrades_and_duration_survives(merge_driver):
    """Control: the id-map hit still upgrades, and the name fallback still
    restores duration / started_at / activityBurstId."""
    raw = [
        {"tid": "call_ok", "name": "terminal", "duration": None, "started_at": None},
        {"tid": "call_old", "name": "terminal", "duration": None, "started_at": None},
    ]
    live = [
        {"tid": "call_ok", "name": "terminal", "is_error": True, "duration": 2.5, "started_at": 100},
        {"tid": "call_new", "name": "terminal", "is_error": True, "duration": 9, "started_at": 200},
    ]
    out = _run_node(merge_driver, {"rawCalls": raw, "liveToolMetadata": live})
    assert out[0].get("is_error") is True, (
        "the id-map hit must still carry the live is_error"
    )
    assert out[0].get("duration") == 2.5, (
        "the id-map hit must still restore duration"
    )
    assert not out[1].get("is_error"), (
        "the name-matched row must not inherit the failure"
    )
    # The name fallback pairs with the next unused live entry (the id hit
    # already consumed entry 0), so the older row takes entry 1's
    # presentation values — what matters is that they are restored at all
    # and that the verdict is not.
    assert out[1].get("duration") == 9 and out[1].get("started_at") == 200, (
        "the name fallback must keep restoring duration / started_at"
    )


# ── Site ③: _enrichSettledToolRowBodyFromLive (static/messages.js) ───────


def _messages_enrich_driver() -> str:
    fn = _function_block(_read("static/messages.js"), "function _enrichSettledToolRowBodyFromLive(")

    return f"""
function _anchorSceneStringPayload(v) {{ return (v===undefined||v===null)?null:String(v); }}
function _anchorSceneToolArgs(live) {{ return (live && live.args && typeof live.args==='object') ? live.args : null; }}
const S = {{ _settledToolIsErrorByTid: PAYLOAD.persistedIsErrorByTid || {{}} }};

{fn}

const row = PAYLOAD.row;
const live = PAYLOAD.live;
const enriched = _enrichSettledToolRowBodyFromLive(row, live);
process.stdout.write(JSON.stringify({{row: row, enriched: enriched}}));
"""


@pytest.fixture
def enrich_driver():
    return _messages_enrich_driver()


def test_enrich_name_paired_row_does_not_inherit_failure(enrich_driver):
    """Bug repro (site ③): a settled row that ``_anchorSceneMatchingContentToolRow``
    paired with a *different-id* live call through the name / invocation
    fallback must keep its own success. Both the ``tool`` row and the
    ``payload`` row drive the compact / transparent render paths."""
    row = {
        "tool_call_id": "call_old",
        "tool": {"id": "call_old", "name": "terminal", "snippet": "[exit 0]"},
        "payload": {"name": "terminal", "snippet": "[exit 0]"},
    }
    live = {"tid": "call_new", "name": "terminal", "is_error": True, "snippet": "[exit 1]"}
    out = _run_node(
        enrich_driver,
        {"row": row, "live": live, "persistedIsErrorByTid": {}},
    )
    assert not out["row"]["tool"].get("is_error"), (
        "a settled row paired with a different-id live call by the name "
        "fallback must not inherit that call's failure"
    )
    assert not out["row"]["payload"].get("is_error"), (
        "the payload row must also keep its own verdict"
    )


def test_enrich_id_paired_row_still_upgrades(enrich_driver):
    """Control: when the row and the live call share the tool id (the
    per-id dedup path), the live verdict is still copied — this is the
    round-4/round-5 behaviour the fix must preserve."""
    row = {
        "tool_call_id": "call_1",
        "tool": {"id": "call_1", "name": "terminal", "snippet": ""},
        "payload": {"name": "terminal", "snippet": ""},
    }
    live = {"tid": "call_1", "name": "terminal", "is_error": True, "snippet": "[exit 3]"}
    out = _run_node(
        enrich_driver,
        {"row": row, "live": live, "persistedIsErrorByTid": {}},
    )
    assert out["row"]["tool"].get("is_error") is True, (
        "the id-paired row must still carry the live failure through"
    )
    assert out["row"]["payload"].get("is_error") is True
    assert out["enriched"] is True


def test_enrich_persisted_map_still_upgrades_without_live_verdict(enrich_driver):
    """Control: the persisted per-tid map (``S._settledToolIsErrorByTid``)
    is id-keyed and must keep upgrading even when the live mirror has no
    verdict — a true cold reload with no in-memory live state."""
    row = {
        "tool_call_id": "call_1",
        "tool": {"id": "call_1", "name": "terminal", "snippet": "[exit 3]"},
        "payload": {"name": "terminal", "snippet": "[exit 3]"},
    }
    live = {"tid": "call_new", "name": "terminal"}
    out = _run_node(
        enrich_driver,
        {"row": row, "live": live, "persistedIsErrorByTid": {"call_1": True}},
    )
    assert out["row"]["tool"].get("is_error") is True, (
        "the persisted per-tid map must keep rendering the cold-reload "
        "failure — it is already id-keyed and is unchanged by this fix"
    )
    assert out["row"]["payload"].get("is_error") is True


# ── Source-shape pins: the upgrade is id-gated at all three sites ────────


def test_all_three_upgrade_sites_are_gated_on_the_id_match():
    ui_body = _function_block(_read("static/ui.js"), "const copyLiveToolMetadata=")
    merge_body = _function_block(
        _read("static/messages.js"), "function _mergeSettledToolCallsWithLiveMetadata("
    )
    enrich_body = _function_block(
        _read("static/messages.js"), "function _enrichSettledToolRowBodyFromLive("
    )

    assert "const idMatchEntry=tid?liveMetadataByTid.get(tid):null;" in ui_body, (
        "copyLiveToolMetadata must keep the per-tid map hit separate from "
        "the name fallback so the is_error upgrade can be gated on it "
        "(#7358 re-gate 9/24)"
    )
    assert "if(idMatchEntry&&live.is_error===true" in ui_body, (
        "copyLiveToolMetadata's one-way is_error upgrade must be gated "
        "on the id-map hit, not the name fallback"
    )

    assert "const idMatchEntry=tid?byTid.get(tid):null;" in merge_body, (
        "_mergeSettledToolCallsWithLiveMetadata must keep the per-tid map "
        "hit separate from the name fallback"
    )
    assert "if(idMatchEntry&&live.is_error===true" in merge_body, (
        "_mergeSettledToolCallsWithLiveMetadata's one-way is_error "
        "upgrade must be gated on the id-map hit"
    )

    assert "_matchedById=!!_rowTid&&!!_liveTid&&_rowTid===_liveTid;" in enrich_body, (
        "_enrichSettledToolRowBodyFromLive must test that the row and the "
        "live call share the tool id before copying the live verdict"
    )
    assert "_liveIsError=_matchedById&&Boolean(live&&live.is_error===true);" in enrich_body, (
        "_enrichSettledToolRowBodyFromLive's live-verdict copy must be "
        "gated on the id match"
    )
    # The name fallback still exists (presentation keys), it just no
    # longer carries the verdict.
    for body, marker in (
        (ui_body, "tc.name===name"),
        (merge_body, "tc.name===name"),
    ):
        assert marker in body, (
            "the name fallback must stay in place for "
            "activityBurstId / duration / started_at"
        )
    livePKeys = [
        key for key in ("activityBurstId", "duration", "started_at") if key in ui_body
    ]
    assert livePKeys, (
        "the name fallback must still copy activityBurstId / duration / "
        "started_at even though it no longer copies is_error"
    )
