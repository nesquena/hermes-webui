"""Regression checks for Hindsight state across profile switches.

Includes both source-structure checks (kept for refactor safety) and
observable-behavior checks that drive the REAL panels.js functions in a Node
VM, proving the reset actually clears state and that in-flight fetches drop
stale responses.

The behavioral tests here deliberately extract and execute the shipped
function bodies rather than re-implementing the guard logic in the test: an
earlier version of this file asserted that a guard expression it wrote itself
evaluated correctly, which passed even when the production loader had no
guard at all.
"""
from pathlib import Path
import re
import shutil
import subprocess
import json
import textwrap

import pytest


PANELS = (Path(__file__).resolve().parents[1] / "static" / "panels.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _extract_function(name: str) -> str:
    """Return the shipped source of a top-level (async) function from panels.js."""
    for marker in (f"async function {name}(", f"function {name}("):
        start = PANELS.find(marker)
        if start != -1:
            end = PANELS.index("\n}\n", start) + 3
            return PANELS[start:end]
    raise AssertionError(f"could not locate {name} in panels.js")


def test_profile_switch_invalidates_hindsight_state_and_late_requests():
    assert "function _resetHindsightState()" in PANELS
    reset_body = _extract_function("_resetHindsightState")
    for state in (
        "_hindsightResults = []",
        "_hindsightReflectText = ''",
        "_hindsightLastQuery = ''",
        "_hindsightReflectQuery = ''",
        "++_hindsightStatusSeq",
        "++_hindsightRecallSeq",
        "++_hindsightReflectSeq",
        "++_hindsightMemoriesSeq",
        "++_hindsightRetainSeq",
    ):
        assert state in reset_body, f"{state} missing from _resetHindsightState"
    switch_start = PANELS.index("async function _profileSwitchPanelLoad()")
    switch_body = PANELS[switch_start:PANELS.index("\n}\n", switch_start) + 2]
    assert "_resetHindsightState();" in switch_body


def test_every_hindsight_fetch_has_both_a_sequence_and_a_profile_guard():
    """Every async Hindsight loader must drop BOTH stale-sequence and
    cross-profile responses.

    The previous version of this test checked only two of the five loaders and
    only asserted the profile guard, so `loadHindsightStatus` shipped with no
    sequence guard at all: two overlapping in-profile status loads could
    resolve out of order and a slow earlier failure would repaint the panel as
    unreachable after a newer success. docs/hindsight.md states the panel
    "uses request sequencing and profile-scoped cache keys so a late response
    or profile switch cannot overwrite the active profile's view" — that is a
    claim about every fetch, so assert it against every fetch.
    """
    loaders = {
        "loadHindsightStatus": "_hindsightStatusSeq",
        "loadHindsightMemories": "_hindsightMemoriesSeq",
        "recallHindsight": "_hindsightRecallSeq",
        "reflectHindsight": "_hindsightReflectSeq",
        "retainHindsight": "_hindsightRetainSeq",
    }
    for function_name, seq_var in loaders.items():
        body = _extract_function(function_name)
        assert f"const seq = ++{seq_var};" in body, (
            f"{function_name} never claims a sequence number from {seq_var}"
        )
        assert f"seq !== {seq_var}" in body, (
            f"{function_name} does not drop stale-sequence responses"
        )
        assert "profile !== _hindsightProfileName()" in body, (
            f"{function_name} does not drop cross-profile responses"
        )


# ── Behavioral tests: drive the actual shipped JS ──

def _run_js(code: str) -> dict:
    """Run JS in Node and return its JSON result.

    A missing Node binary skips the test. Anything else — a JS exception, a
    non-zero exit, unparseable output — is a FAILURE, not a skip: an earlier
    version treated every Node error as "skipped" and silently fell back to a
    substring assertion, so a harness that could no longer run still reported
    a pass.
    """
    if NODE is None:
        pytest.skip("node is not installed")
    result = subprocess.run([NODE, "-e", code], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, f"node exited {result.returncode}:\n{result.stderr}"
    try:
        return json.loads(result.stdout.strip() or "{}")
    except json.JSONDecodeError as error:
        raise AssertionError(
            f"node produced non-JSON output:\n{result.stdout}\n{result.stderr}"
        ) from error


def test_hindsight_reset_clears_observable_state_behavioral():
    """Drive the real _resetHindsightState and verify observable state clears."""
    js = textwrap.dedent(
        """
        let _hindsightResults = [{text: 'leaked'}];
        let _hindsightReflectText = 'leaked';
        let _hindsightReflectQuery = 'leaked';
        let _hindsightLastQuery = 'leaked';
        let _hindsightStatus = {enabled: true};
        let _hindsightMemories = [{id: '1'}];
        let _hindsightError = 'err';
        let _hindsightReflectError = 'err';
        let _hindsightListError = 'err';
        let _hindsightStatusSeq = 5;
        let _hindsightRecallSeq = 5;
        let _hindsightReflectSeq = 5;
        let _hindsightMemoriesSeq = 5;
        let _hindsightRetainSeq = 5;
        let _hindsightStatusProfile = 'old';
        let _hindsightMemoriesProfile = 'old';
        let _hindsightLastElapsed = 99;
        let _hindsightMemoriesTotal = 1;
        let _hindsightLoading = true;
        let _hindsightReflectLoading = true;
        let _hindsightMemoriesLoading = true;
        let _hindsightRetainLoading = true;
        """
    ) + _extract_function("_resetHindsightState") + textwrap.dedent(
        """
        _resetHindsightState();
        console.log(JSON.stringify({
            results: _hindsightResults.length,
            reflectText: _hindsightReflectText,
            reflectQuery: _hindsightReflectQuery,
            lastQuery: _hindsightLastQuery,
            status: _hindsightStatus,
            memories: _hindsightMemories.length,
            error: _hindsightError,
            statusSeq: _hindsightStatusSeq,
            recallSeq: _hindsightRecallSeq,
            reflectSeq: _hindsightReflectSeq,
            memoriesSeq: _hindsightMemoriesSeq,
            retainSeq: _hindsightRetainSeq,
        }));
        """
    )
    out = _run_js(js)
    assert out == {
        "results": 0,
        "reflectText": "",
        "reflectQuery": "",
        "lastQuery": "",
        "status": None,
        "memories": 0,
        "error": "",
        "statusSeq": 6,
        "recallSeq": 6,
        "reflectSeq": 6,
        "memoriesSeq": 6,
        "retainSeq": 6,
    }, out


def test_status_load_drops_a_late_response_that_lost_the_race():
    """An older in-flight status load must not overwrite a newer one.

    Drives the REAL loadHindsightStatus twice with overlapping requests and
    resolves them out of order (the newer one first). Without a sequence
    guard the stale earlier response wins and the panel shows a false
    'unreachable' after a newer success.
    """
    js = textwrap.dedent(
        """
        let _hindsightStatus = null;
        let _hindsightStatusProfile = '';
        let _hindsightStatusSeq = 0;
        let _memoryData = { hindsight_enabled: true };
        const _hindsightProfileName = () => 'profileA';

        let resolveFirst, resolveSecond;
        let calls = 0;
        const api = () => {
            calls += 1;
            if (calls === 1) return new Promise((res, rej) => { resolveFirst = rej; });
            return new Promise((res) => { resolveSecond = res; });
        };
        """
    ) + _extract_function("loadHindsightStatus") + textwrap.dedent(
        """
        (async () => {
            const first = loadHindsightStatus(true);   // stale, will fail slowly
            const second = loadHindsightStatus(true);  // newer, succeeds first
            resolveSecond({ enabled: true, reachable: true, marker: 'newer' });
            await second;
            const afterNewer = _hindsightStatus && _hindsightStatus.marker;
            resolveFirst(new Error('stale failure'));
            await first;
            console.log(JSON.stringify({
                afterNewer: afterNewer || null,
                finalMarker: (_hindsightStatus && _hindsightStatus.marker) || null,
                finalReachable: (_hindsightStatus && _hindsightStatus.reachable) || false,
            }));
        })();
        """
    )
    out = _run_js(js)
    assert out["afterNewer"] == "newer", out
    # The stale rejection must be discarded, leaving the newer success intact.
    assert out["finalMarker"] == "newer", f"stale status response overwrote a newer one: {out}"
    assert out["finalReachable"] is True, out


def test_hindsight_copy_is_owned_by_english_locale_fallback():
    """Untranslated Hindsight UI copy must use the established English fallback."""
    i18n = (Path(__file__).resolve().parents[1] / "static" / "i18n.js").read_text(
        encoding="utf-8"
    )
    matches = list(
        re.finditer(r"^  ('[^']+'|[A-Za-z][A-Za-z0-9-]*): \{$", i18n, re.MULTILINE)
    )
    end = i18n.index("\n};", matches[-1].start())
    blocks = {
        match.group(1).strip("'"): i18n[
            match.start() : (matches[index + 1].start() if index + 1 < len(matches) else end)
        ]
        for index, match in enumerate(matches)
    }
    keys = tuple(sorted(set(re.findall(r"\b(hindsight_[a-z_]+):", blocks["en"]))))
    assert keys
    for locale, block in blocks.items():
        if locale == "en":
            continue
        for key in keys:
            assert not re.search(rf"\b{re.escape(key)}:\s*", block), (
                f"{key!r} must fall back to English instead of being duplicated in {locale!r}"
            )
    assert "_locale[key] ?? LOCALES.en[key]" in i18n


def test_memories_load_drops_a_late_response_that_lost_the_race():
    """Same contract for the recent-memories loader, driven end to end."""
    js = textwrap.dedent(
        """
        let _hindsightMemories = [];
        let _hindsightMemoriesTotal = 0;
        let _hindsightMemoriesLoading = false;
        let _hindsightMemoriesProfile = '';
        let _hindsightMemoriesSeq = 0;
        let _hindsightListError = '';
        let _memoryData = { hindsight_enabled: true };
        const _hindsightProfileName = () => 'profileA';
        const _renderHindsight = () => {};

        let resolveFirst, resolveSecond;
        let calls = 0;
        const api = () => {
            calls += 1;
            if (calls === 1) return new Promise((res) => { resolveFirst = res; });
            return new Promise((res) => { resolveSecond = res; });
        };
        """
    ) + _extract_function("loadHindsightMemories") + textwrap.dedent(
        """
        (async () => {
            const first = loadHindsightMemories(true);
            const second = loadHindsightMemories(true);
            resolveSecond({ memories: [{ id: 'newer' }], total: 1 });
            await second;
            resolveFirst({ memories: [{ id: 'stale' }, { id: 'stale2' }], total: 2 });
            await first;
            console.log(JSON.stringify({
                ids: _hindsightMemories.map(m => m.id),
                total: _hindsightMemoriesTotal,
            }));
        })();
        """
    )
    out = _run_js(js)
    assert out["ids"] == ["newer"], f"stale memories response overwrote a newer one: {out}"
    assert out["total"] == 1, out
