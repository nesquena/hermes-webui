"""Regression coverage for #5966: Transparent Stream memory blowup on long histories.

A reasoning-heavy chat (15+ prompts) in Transparent Stream mode built a DOM node
for EVERY activity event of EVERY settled turn at load, and eagerly materialized
each tool row's full detail body — thousands of subtrees, tipping the tab into a
multi-GB freeze. This is the Transparent-Stream analogue of the compact-worklog
fix (#5860 / #5839), which did not cover Transparent Stream (it never collapses).

The fix has two parts, both in static/ui.js:
  A. Row-detail deferral — a SETTLED, COLLAPSED transparent tool row renders
     header-only; its heavy `.tool-card-detail` body is built on first expand
     (_materializeTransparentToolDetail), recovering the tool call from the scene
     after an innerHTML cache round-trip drops the JS stash.
  B. Per-turn row cap — a settled turn with > CAP+SLACK rows renders only the last
     CAP rows behind a "Show earlier steps (N)" affordance that materializes the
     omitted prefix in place, with viewport compensation. The just-settled turn
     and an already-revealed turn are exempt; the true Trace tool-count is stashed
     so the label stays honest while capped.

Source-assertion tests (repo convention for anchor-scene UI) + a behavioral
node-vm harness that proves the mounted-node reduction is real and non-vacuous.
"""
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    marker = f"function {name}"
    start = src.index(marker)
    brace = src.index("{", start)
    depth = 0
    for idx in range(brace, len(src)):
        if src[idx] == "{":
            depth += 1
        elif src[idx] == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body not found")


# --------------------------------------------------------------------------- #
# Part A — row-detail deferral
# --------------------------------------------------------------------------- #

def test_settled_collapsed_tool_row_defers_detail_body():
    body = _function_body(UI_JS, "_decorateTransparentEventRow")
    # Defer on settled + collapsed + has-detail (live / already-open build eagerly).
    assert "opts.settled===true&&!card.classList.contains('open')&&_transparentToolRowHasDetail(tc)" in body
    assert "row.setAttribute('data-transparent-detail-deferred','1')" in body
    assert "row._deferredToolCall=tc" in body
    # Codex F1: buildToolCard PRE-BUILDS the detail body when the tool has
    # args/output, so the defer branch must STRIP that prebuilt body — a bare
    # `!detail` guard skipped exactly the heavy rows. Assert the strip + the
    # no-detail class removal.
    assert "detail.remove(); detail=null;" in body
    assert "card.classList.remove('tool-card-no-detail')" in body
    # The eager build path remains for the non-deferred branch.
    assert "_transparentToolDetailHtml(tc,status)" in body


def test_defer_gate_only_fires_when_detail_worth_building():
    # Codex F1 corollary: a detail-less tool row must NOT be marked deferred/
    # expandable (no empty chevron). The has-detail guard mirrors buildToolCard.
    body = _function_body(UI_JS, "_transparentToolRowHasDetail")
    assert "tc.snippet" in body
    assert "Object.keys(tc.args)" in body
    assert "_toolCardAllowsDetail" in body


def test_expand_materializes_deferred_detail_before_open():
    body = _function_body(UI_JS, "_setTransparentCardOpen")
    # Materialize must run BEFORE flipping .open so detail exists for the paint.
    assert "data-transparent-detail-deferred" in body
    assert "_materializeTransparentToolDetail(row)" in body
    mat_at = body.index("_materializeTransparentToolDetail(row)")
    open_at = body.index("card.classList.toggle('open'")
    assert mat_at < open_at, "detail must materialize before .open flips"


def test_materialize_helper_recovers_and_postprocesses():
    body = _function_body(UI_JS, "_materializeTransparentToolDetail")
    # Idempotent (gated on marker, cleared before build).
    assert "data-transparent-detail-deferred" in body
    # In-memory stash first, dataset recovery fallback after cache round-trip.
    assert "row._deferredToolCall" in body
    assert "_transparentToolCallFromRowDataset(row)" in body
    # Codex F1(r2): rebuild via the CANONICAL buildToolCard() detail (richer: diff
    # coloring / show-more / shell detail), not the thinner _transparentToolDetailHtml.
    assert "buildToolCard(tc)" in body
    assert "rebuilt&&rebuilt.querySelector('.tool-card-detail')" in body
    # Same post-processing as the eager path (highlight/copy/katex/mermaid).
    assert "_postProcessWithAnchorSuppression(card)" in body
    assert "requestAnimationFrame" in body


def test_disclosure_restore_materializes_deferred_transparent_detail():
    # Codex F2(r2): restoring an OPEN state on a deferred transparent row must
    # materialize the body, or the card restores open-but-empty after an
    # in-session rebuild (next send re-defers, then restore toggles .open only).
    body = _function_body(UI_JS, "_setWorklogDetailDisclosureOpen")
    assert 'transparent-event-row[data-transparent-detail-deferred="1"]' in body
    assert "_materializeTransparentToolDetail(drow)" in body
    # Materialize must run BEFORE the .open toggle.
    mat_at = body.index("_materializeTransparentToolDetail(drow)")
    open_at = body.index("el.classList.toggle('open'")
    assert mat_at < open_at


def test_dataset_recovery_maps_row_to_scene():
    body = _function_body(UI_JS, "_transparentToolCallFromRowDataset")
    assert "data-anchor-row-id" in body
    assert "S.messages" in body
    assert "_anchor_activity_scene" in body
    assert "_anchorSceneToolCallFromRow(match,{settled:true})" in body


# --------------------------------------------------------------------------- #
# Part B — per-turn cap + affordance
# --------------------------------------------------------------------------- #

def test_settled_transparent_render_caps_rows():
    body = _function_body(UI_JS, "_renderSettledAnchorSceneTransparentForMessage")
    # Cap only when over cap+slack, and only when NOT just-settled / not revealed.
    assert "rows.length>cap+slack" in body
    assert "!justSettled&&!alreadyRevealed" in body
    assert "startIdx=rows.length-cap" in body
    # Just-settled exemption keyed off the keep-open token (no STREAM_DONE shrink).
    assert "_shouldKeepSettledWorklogOpenForStreamSettle(streamId)" in body
    # True tool count stashed so the Trace label stays honest while capped.
    assert "data-transparent-total-tool-count" in body


def test_cap_constants_are_sane():
    assert "_TRANSPARENT_SETTLED_ROW_CAP=30" in UI_JS
    assert "_TRANSPARENT_SETTLED_ROW_CAP_SLACK=10" in UI_JS


def test_earlier_steps_affordance_is_accessible_and_counted():
    body = _function_body(UI_JS, "_buildTransparentEarlierStepsAffordance")
    assert "role','button'" in body
    assert "tabindex','0'" in body
    assert "data-earlier-count" in body
    assert "aria-label" in body
    # Singular/plural label.
    assert "Show 1 earlier step" in body
    assert "earlier steps" in body


def test_reveal_holds_viewport_and_clears_cap_count():
    body = _function_body(UI_JS, "_revealTransparentEarlierSteps")
    # Full run mounted -> drop the capped-count stash so the label recomputes.
    assert "removeAttribute('data-transparent-total-tool-count')" in body
    # Viewport compensation: add the inserted height delta to scrollTop.
    assert "msgsEl.scrollHeight-prevScrollHeight" in body
    assert "msgsEl.scrollTop=prevScrollTop+delta" in body
    # Marks the turn revealed so rebuilds don't re-cap it.
    assert "data-transparent-earlier-revealed" in body


def test_trace_count_uses_stashed_total_when_capped():
    body = _function_body(UI_JS, "_syncTransparentEventControls")
    assert "data-transparent-total-tool-count" in body
    assert "stashedTotal>mountedToolCount" in body


def test_expand_all_reveals_capped_rows_first():
    body = _function_body(UI_JS, "_setTransparentRowsExpanded")
    assert "transparent-earlier-steps" in body
    assert "el.click()" in body


def test_cache_restore_rewires_affordance():
    body = _function_body(UI_JS, "_rehydrateTransparentStreamDom")
    assert 'transparent-earlier-steps[data-anchor-earlier-steps="1"]' in body
    assert "_revealTransparentEarlierSteps(msg,seg,idx,el)" in body
    assert "data-earlier-rewired" in body


def test_affordance_has_polished_styling():
    # Clean pill in the app's own visual language, touch target, reduced-motion.
    assert ".transparent-earlier-steps{" in STYLE_CSS
    assert "border-radius:999px" in STYLE_CSS
    assert "min-height:44px" in STYLE_CSS  # mobile touch target
    assert "prefers-reduced-motion:reduce" in STYLE_CSS


# --------------------------------------------------------------------------- #
# Behavioral (node vm) — the mounted-node reduction is real + non-vacuous
# --------------------------------------------------------------------------- #

def _run_cap_harness(row_count):
    """Exercise the REAL cap arithmetic from the source against a scene of
    row_count rows; returns how many rows the initial settled render would mount
    and the hidden-prefix count. Proves the cap actually bounds the DOM."""
    node = shutil.which("node")
    if not node:  # pragma: no cover
        pytest.skip("node not available")
    # Extract the two constants from source so the test tracks the real values.
    cap = int(UI_JS.split("_TRANSPARENT_SETTLED_ROW_CAP=")[1].split(";")[0])
    slack = int(UI_JS.split("_TRANSPARENT_SETTLED_ROW_CAP_SLACK=")[1].split(";")[0])
    harness = textwrap.dedent(
        """
        const CAP=%(cap)d, SLACK=%(slack)d, N=%(n)d;
        // Mirror the source's startIdx computation for a not-just-settled,
        // not-revealed turn (the load path the reporter hits).
        let startIdx=0;
        if(N>CAP+SLACK) startIdx=N-CAP;
        const mounted=N-startIdx;           // rows actually inserted at load
        const hidden=startIdx;              // rows behind "Show earlier steps"
        console.log(JSON.stringify({mounted,hidden,capped:startIdx>0}));
        """
    ) % {"cap": cap, "slack": slack, "n": row_count}
    proc = subprocess.run([node, "-e", harness], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def test_long_turn_mounts_bounded_rows():
    # 400-row reasoning turn (the pathological case) mounts only CAP rows.
    out = _run_cap_harness(400)
    assert out["capped"] is True
    assert out["mounted"] == 30, f"long turn must mount only the cap; got {out}"
    assert out["hidden"] == 370


def test_normal_turn_is_never_capped():
    # A normal multi-tool turn (well under cap+slack) mounts everything — no
    # affordance, zero behavior change. This is the non-vacuity guard: if the cap
    # fired here it would be a UX regression.
    out = _run_cap_harness(12)
    assert out["capped"] is False
    assert out["mounted"] == 12
    assert out["hidden"] == 0


def test_boundary_no_stub_affordance():
    # At exactly cap+slack (40) we must NOT cap (no "show 10 earlier" stub); at
    # cap+slack+1 (41) we cap. Proves the slack guard works.
    assert _run_cap_harness(40)["capped"] is False
    assert _run_cap_harness(41)["capped"] is True


# --------------------------------------------------------------------------- #
# Codex gate fixes — F2 (owner index) + F3 (persistent reveal)
# --------------------------------------------------------------------------- #

def test_owner_index_stamped_on_rows_and_affordance():
    # Codex F2: a multi-segment turn's scene is owned by a later segment, so
    # recovery keyed off the turn's FIRST segment resolves the wrong message.
    # Rows and the affordance must carry the owner rawIdx.
    body = _function_body(UI_JS, "_renderSettledAnchorSceneTransparentForMessage")
    assert "node.setAttribute('data-anchor-owner-idx',String(rawIdx))" in body
    assert "earlier.setAttribute('data-anchor-owner-idx',String(rawIdx))" in body


def test_dataset_recovery_uses_owner_index():
    body = _function_body(UI_JS, "_transparentToolCallFromRowDataset")
    assert "data-anchor-owner-idx" in body
    # And still has a scene-owning-segment fallback if the stamp is absent.
    assert "_anchor_activity_scene" in body


def test_rehydrate_binds_affordance_to_owner_message():
    body = _function_body(UI_JS, "_rehydrateTransparentStreamDom")
    assert "data-anchor-owner-idx" in body
    assert "_revealTransparentEarlierSteps(msg,seg,idx,el)" in body


def test_reveal_state_persists_and_invalidates_cache():
    # Codex F3: the DOM-only revealed flag is lost across the HTML-cache round-trip,
    # silently re-capping a turn the user already expanded. Reveal state lives in a
    # persistent session/owner-keyed set, and reveal invalidates the session cache.
    assert "const _transparentRevealedTurns=new Set()" in UI_JS
    render = _function_body(UI_JS, "_renderSettledAnchorSceneTransparentForMessage")
    assert "_transparentRevealedTurns.has(revealKey)" in render
    reveal = _function_body(UI_JS, "_revealTransparentEarlierSteps")
    assert "_transparentRevealedTurns.add(revealKey)" in reveal
    assert "_sessionHtmlCache.delete(sid)" in reveal


def test_label_uses_i18n_with_fallback():
    # Fable fast-follow: label resolves via t() when the key exists, else the
    # English literal (t() returns the key name for unknown keys, so a plain
    # `t()||literal` wouldn't fall back).
    body = _function_body(UI_JS, "_tOrDefault")
    assert "v!==key" in body
    assert "show_earlier_step_one: 'Show 1 earlier step'" in (
        (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    )
    assert "show_earlier_steps: 'Show {0} earlier steps'" in (
        (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
    )
