"""Regression tests for issue #6612: update_channel autosave ownership.

The fix removes update_channel from the generic preferences autosave payload
(_preferencesPayloadFromUi) and gives the settingsUpdateChannel selector a
dedicated write path (_saveUpdateChannelFromSelector) that sends only
update_channel and re-syncs the selector from the confirmed server response.

Node tests execute the real JS under controlled stubs; server-side tests call
save_settings() directly. Node tests are skipped when node is not on PATH.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Module-scope sources and fixtures
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.resolve()
PANELS_JS_PATH = REPO_ROOT / "static" / "panels.js"
PANELS_JS = PANELS_JS_PATH.read_text(encoding="utf-8")

_REPRO_PATH = REPO_ROOT / "tests" / "fixtures" / "issue6612_update_channel_repro.json"
with _REPRO_PATH.open(encoding="utf-8") as _f:
    _REPRO = json.load(_f)

STALE_AUTOSAVE_PAYLOAD = _REPRO["stale_autosave_payload"]
EXPLICIT_CHANNEL_PAYLOADS = _REPRO["explicit_channel_payloads"]
NORMALIZATION_CASES = _REPRO["normalization_cases"]
EXPECTED_AFTER_STALE = _REPRO["expected_persisted_channel_after_stale_autosave"]
SEQUENCE = _REPRO["sequence"]

NODE = shutil.which("node")


def _run_node(source: str) -> str:
    result = subprocess.run(
        [NODE],
        input=source,
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def _base_panels_js() -> str:
    r = subprocess.run(
        ["git", "show", "320789ae:static/panels.js"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        encoding="utf-8",
        timeout=30,
    )
    if r.returncode != 0:
        pytest.skip(f"cannot read base panels.js: {r.stderr.strip()}")
    return r.stdout


def _node_prelude(panels_src: str) -> str:
    """Embed panels_src and define extractFunc. Sets _channelSaveSeq and
    _confirmedUpdateChannel on global so the extracted async function can
    access them via indirect eval (global scope)."""
    return f"""
const panelsSrc = {panels_src!r};
function extractFunc(src, name) {{
  const re = new RegExp('(?:async\\\\s+)?function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found in source');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
// Module-scope variables referenced as free variables inside the extracted functions.
global._channelSaveSeq = 0;
global._confirmedUpdateChannel = null;
global._settingsPanelPostQueue = Promise.resolve();
"""


def _preference_payload_script(panels_src: str, stale_selector_value: str = "experimental") -> str:
    """Node script that executes _preferencesPayloadFromUi() under controlled stubs.

    Stubs $() to return:
    - settingsShowTps         -> {checked: true}            (unrelated preference)
    - settingsUpdateChannel   -> {value: stale_selector}    (stale or current value)
    - all other selectors     -> null

    On base code: payload.update_channel = stale_selector_value (the bug).
    On head code: selector never read -> update_channel absent (the fix).
    """
    return _node_prelude(panels_src) + f"""
global.$ = function(id) {{
  if (id === 'settingsShowTps') return {{ checked: true }};
  if (id === 'settingsUpdateChannel') return {{ value: {json.dumps(stale_selector_value)} }};
  return null;
}};
global._speechPreferencesPayloadFromUi = function() {{ return {{}}; }};
global._preferencesPayloadFromUi = (0, eval)('(' + extractFunc(panelsSrc, '_preferencesPayloadFromUi') + ')');
const payload = _preferencesPayloadFromUi();
console.log(JSON.stringify(payload));
"""


def _function_block(src: str, name: str) -> str:
    marker = re.search(
        rf"(^|\n)(?:async\s+)?function\s+{re.escape(name)}\(", src
    )
    assert marker is not None, f"{name}() not found in panels.js"
    start = marker.start()
    next_marker = re.search(
        r"\n(?:function\s+\w+\(|async\s+function\s+\w+\()", src[start + 1:]
    )
    end = start + 1 + next_marker.start() if next_marker else len(src)
    return src[start:end]


def _channel_writer_script_prelude(panels_src: str) -> str:
    """Shared prelude for node tests that exercise _saveUpdateChannelFromSelector."""
    return _node_prelude(panels_src) + """
global._saveUpdateChannelFromSelector = (0, eval)(
  '(' + extractFunc(panelsSrc, '_saveUpdateChannelFromSelector') + ')'
);
global._enqueueSettingsPost = (0, eval)(
  '(' + extractFunc(panelsSrc, '_enqueueSettingsPost') + ')'
);
"""


# ---------------------------------------------------------------------------
# 1. Reproduction: stale tab overwrite
#    Drive the full REPRO["sequence"] step-by-step.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_payload_builder_excludes_update_channel_head():
    """Head: _preferencesPayloadFromUi() must not return update_channel even
    when the stale selector stub holds 'experimental' (SEQUENCE[0]).

    base-fails/head-passes behavioral pair from the actual builder.
    """
    head_payload = json.loads(_run_node(_preference_payload_script(PANELS_JS)))
    assert "update_channel" not in head_payload, (
        f"head: builder must not return update_channel; "
        f"got keys: {list(head_payload.keys())}"
    )
    assert head_payload.get("show_tps") is True, (
        f"head: show_tps must be in payload (builder ran); got {head_payload!r}"
    )

    base_js = _base_panels_js()
    base_payload = json.loads(_run_node(_preference_payload_script(base_js)))
    assert base_payload.get("update_channel") == "experimental", (
        f"base: builder must return update_channel='experimental'; got {base_payload!r}"
    )


def test_sequence_replay(tmp_path, monkeypatch):
    """Replay the full REPRO sequence step-by-step.

    SEQUENCE[0]: Tab A holds 'experimental' (stale selector value).
    SEQUENCE[1]: Tab B saves Stable.
    SEQUENCE[2-3]: Tab A fires a generic autosave (unrelated preference only;
                   no update_channel on head because the builder no longer reads
                   the stale selector).
    SEQUENCE[4]: On head the channel stays 'stable' (base: would flip back).
    """
    import api.config as config
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    # SEQUENCE[0]: Tab A's selector is stale 'experimental'. Verify via builder
    # that the generic autosave payload would NOT carry that value on head.
    if NODE is not None:
        tab_a_payload = json.loads(
            _run_node(_preference_payload_script(PANELS_JS, stale_selector_value="experimental"))
        )
        assert "update_channel" not in tab_a_payload, (
            f"SEQUENCE[0]: {SEQUENCE[0]!r}\n"
            f"builder payload must not carry update_channel; got {tab_a_payload!r}"
        )

    # SEQUENCE[1]: Tab B selects Stable and saves.
    config.save_settings({"update_channel": "stable"})
    after_tab_b = config.load_settings().get("update_channel")
    assert after_tab_b == "stable", (
        f"SEQUENCE[1]: {SEQUENCE[1]!r}\n"
        f"expected 'stable', got {after_tab_b!r}"
    )

    # SEQUENCE[2]: Tab A toggles an unrelated preference (TPS display).
    # SEQUENCE[3]: Tab A's generic autosave posts its payload.
    # On head: payload has no update_channel. On base: it would.
    if NODE is not None:
        generic_payload = json.loads(
            _run_node(_preference_payload_script(PANELS_JS, stale_selector_value="experimental"))
        )
        assert "update_channel" not in generic_payload, (
            f"SEQUENCE[2-3]: {SEQUENCE[2]!r}\n"
            f"generic payload must not carry update_channel; got {generic_payload!r}"
        )
        config.save_settings(generic_payload)
    else:
        # node unavailable: use REPRO payload minus the channel key
        config.save_settings(
            {k: v for k, v in STALE_AUTOSAVE_PAYLOAD.items() if k != "update_channel"}
        )

    # SEQUENCE[4]: On head, channel stays 'stable'. Base would flip it back.
    persisted = config.load_settings().get("update_channel", "stable")
    assert persisted == EXPECTED_AFTER_STALE, (
        f"SEQUENCE[4]: {SEQUENCE[4]!r}\n"
        f"expected {EXPECTED_AFTER_STALE!r}, got {persisted!r}"
    )


# ---------------------------------------------------------------------------
# 2. Negative space: explicit channel change
# ---------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_explicit_channel_writer_sends_only_channel():
    """_saveUpdateChannelFromSelector() must POST exactly {update_channel: <val>}
    for each REPRO explicit_channel_payload, and re-sync the selector from the
    confirmed server response.
    """
    script = _channel_writer_script_prelude(PANELS_JS) + """
(async () => {
  const calls = [];
  const statuses = [];
  global._setPreferencesAutosaveStatus = function(s) { statuses.push(s); };
  global.api = async function(url, opts) {
    const body = JSON.parse(opts.body);
    calls.push({ url, method: opts.method, body });
    return { update_channel: body.update_channel };
  };

  const sel1 = { value: 'stable' };
  global._confirmedUpdateChannel = null;
  await _saveUpdateChannelFromSelector(sel1);

  const sel2 = { value: 'experimental' };
  global._confirmedUpdateChannel = 'stable';
  await _saveUpdateChannelFromSelector(sel2);

  // Re-sync: server overrides to 'stable' (e.g., persisted was already stable)
  const sel3 = { value: 'experimental' };
  global._confirmedUpdateChannel = 'stable';
  global.api = async function(url, opts) {
    calls.push({ url, method: opts.method, body: JSON.parse(opts.body) });
    return { update_channel: 'stable' };
  };
  await _saveUpdateChannelFromSelector(sel3);

  console.log(JSON.stringify({ calls, statuses, resyncedValue: sel3.value, confirmedAfterResync: _confirmedUpdateChannel }));
})().catch(err => { console.error(err.message); process.exit(1); });
"""
    result = json.loads(_run_node(script))
    calls = result["calls"]

    assert len(calls) == 3, f"expected 3 api calls, got {len(calls)}: {calls!r}"
    assert calls[0]["url"] == "/api/settings"
    assert calls[0]["method"] == "POST"
    assert calls[0]["body"] == {"update_channel": "stable"}, (
        f"stable call body wrong: {calls[0]['body']!r}"
    )
    assert calls[1]["body"] == {"update_channel": "experimental"}, (
        f"experimental call body wrong: {calls[1]['body']!r}"
    )
    assert result["resyncedValue"] == "stable", (
        f"selector must re-sync to 'stable'; got {result['resyncedValue']!r}"
    )
    assert result["confirmedAfterResync"] == "stable", (
        f"_confirmedUpdateChannel must be updated to 'stable'; got {result['confirmedAfterResync']!r}"
    )
    # Every call should have emitted 'saving' then 'saved'
    assert "saving" in result["statuses"], f"'saving' status not emitted; got {result['statuses']!r}"
    assert "saved" in result["statuses"], f"'saved' status not emitted; got {result['statuses']!r}"

    explicit_vals = {p["update_channel"] for p in EXPLICIT_CHANNEL_PAYLOADS}
    sent_vals = {c["body"]["update_channel"] for c in calls[:2]}
    assert explicit_vals == sent_vals


def test_dedicated_channel_writer_exists():
    """Source shape: _saveUpdateChannelFromSelector exists with the right structure."""
    assert "async function _saveUpdateChannelFromSelector(" in PANELS_JS
    block = _function_block(PANELS_JS, "_saveUpdateChannelFromSelector")
    assert "update_channel" in block
    assert "_enqueueSettingsPost" in block
    assert "channelSel.value" in block
    # Monotonic guard
    assert "_channelSaveSeq" in block
    assert "seq!==_channelSaveSeq" in block or "seq !== _channelSaveSeq" in block
    # Confirmed-value tracking
    assert "_confirmedUpdateChannel" in block
    # saving/saved feedback
    assert "_setPreferencesAutosaveStatus" in block
    assert "'saving'" in block
    assert "'saved'" in block
    assert block.index("_confirmedUpdateChannel=confirmed") < block.index("if(seq!==_channelSaveSeq)"), (
        "confirmed baseline must update before the stale-response UI guard"
    )


def test_settings_panel_posts_use_shared_queue_and_channel_is_single_owner():
    """Every settings-panel writer shares the queue and Save Settings carries no channel."""
    assert PANELS_JS.count("_enqueueSettingsPost({") == 10
    direct_settings_calls = re.findall(r"api\(\s*'/api/settings'\s*,", PANELS_JS)
    assert direct_settings_calls == ["api('/api/settings',"], (
        f"only the queue helper may call settings POST/GET with options; got {direct_settings_calls!r}"
    )
    assert "body.update_channel=" not in _function_block(PANELS_JS, "saveSettings")
    assert "_settingsPanelPostQueue=Promise.resolve()" in PANELS_JS
    producer_blocks = (
        "_autosaveAppearanceSettings",
        "_autosavePreferencesSettings",
        "_saveUpdateChannelFromSelector",
        "handlePluginEnableToggle",
        "_setAuthDisabledAck",
        "saveSettings",
        "goPasswordless",
        "disableAuth",
    )
    for producer in producer_blocks:
        assert "_enqueueSettingsPost" in _function_block(PANELS_JS, producer), producer
    provider_block = _function_block(PANELS_JS, "_attachBudgetControls")
    assert "_enqueueSettingsPost" in provider_block, "provider budget writer must use the queue"


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_settings_post_queue_serializes_and_recovers_after_failure():
    """FIFO queue preserves request order and a rejected request does not poison its tail."""
    script = _channel_writer_script_prelude(PANELS_JS) + """
(async () => {
  const started = [];
  let releaseFirst;
  const firstBlocker = new Promise(resolve => { releaseFirst = resolve; });
  global.api = async function(url, opts) {
    const body = JSON.parse(opts.body);
    started.push(body);
    if (started.length === 1) await firstBlocker;
    return { ok: true };
  };
  const p1 = _enqueueSettingsPost({method: 'POST', body: JSON.stringify({first: true})});
  const p2 = _enqueueSettingsPost({method: 'POST', body: JSON.stringify({second: true})});
  await Promise.resolve();
  const beforeRelease = started.length;
  releaseFirst();
  await Promise.all([p1, p2]);

  global._settingsPanelPostQueue = Promise.resolve();
  let attempts = 0;
  global.api = async function() {
    attempts++;
    return attempts === 1 ? undefined : { ok: true };
  };
  const rejected = _enqueueSettingsPost({method: 'POST', body: '{}'}).then(
    () => 'resolved', err => err.message
  );
  const follows = _enqueueSettingsPost({method: 'POST', body: '{}'});
  console.log(JSON.stringify({
    started,
    beforeRelease,
    rejected: await rejected,
    following: await follows,
    attempts,
  }));
})().catch(err => { console.error(err.stack || err.message); process.exit(1); });
"""
    result = json.loads(_run_node(script))
    assert result["beforeRelease"] == 1, result
    assert result["started"] == [{"first": True}, {"second": True}], result
    assert result["rejected"] == "Invalid settings response", result
    assert result["following"] == {"ok": True}, result
    assert result["attempts"] == 2, result


def test_channel_listener_calls_dedicated_writer():
    """Source shape: settingsUpdateChannel listener calls _saveUpdateChannelFromSelector
    only; checkUpdatesNow and _syncUpdateChannelBadge must be inside the writer."""
    load_block = _function_block(PANELS_JS, "loadSettingsPanel")
    idx = load_block.find("settingsUpdateChannel")
    assert idx != -1
    window = load_block[idx: idx + 600]
    assert "_saveUpdateChannelFromSelector" in window
    assert "_schedulePreferencesAutosave" not in window
    # Side effects must NOT be in the listener anymore; they belong inside the writer.
    assert "checkUpdatesNow" not in window, (
        "checkUpdatesNow must be inside _saveUpdateChannelFromSelector, not in the listener"
    )
    assert "_syncUpdateChannelBadge" not in window, (
        "_syncUpdateChannelBadge must be inside _saveUpdateChannelFromSelector, not in the listener"
    )


def test_explicit_channel_payloads_persist(tmp_path, monkeypatch):
    """Server-side: both REPRO explicit_channel_payloads persist correctly."""
    import api.config as config
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    for payload in EXPLICIT_CHANNEL_PAYLOADS:
        expected = payload["update_channel"]
        result = config.save_settings(payload)
        assert result.get("update_channel") == expected, (
            f"explicit payload {payload!r} must persist {expected!r}; got {result.get('update_channel')!r}"
        )
        assert config.load_settings().get("update_channel") == expected


# ---------------------------------------------------------------------------
# 3. Unrelated preference preservation
# ---------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_unrelated_preference_payload_contains_no_channel():
    """Builder carries show_tps but NOT update_channel, even with stale selector."""
    payload = json.loads(_run_node(_preference_payload_script(PANELS_JS)))
    assert payload.get("show_tps") is True, f"show_tps absent; got {payload!r}"
    assert "update_channel" not in payload, f"update_channel present; keys: {list(payload.keys())}"


def test_unrelated_autosave_does_not_touch_channel(tmp_path, monkeypatch):
    """Server-side: posting an unrelated preference must leave the channel unchanged."""
    import api.config as config
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    config.save_settings({"update_channel": "stable"})
    config.save_settings({"show_tps": True})
    assert config.load_settings().get("update_channel") == "stable"


# ---------------------------------------------------------------------------
# 4. Normalization: invalid/missing values leave the persisted channel unchanged
#    (api/config.py ignores keys whose value is not in _SETTINGS_ENUM_VALUES[k];
#     the persisted value is NOT overwritten or normalized to stable)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("case", NORMALIZATION_CASES)
def test_normalization_invalid_channel_leaves_unchanged(tmp_path, monkeypatch, case):
    """Unknown, empty, and missing update_channel values leave the persisted
    channel unchanged. Seeded to 'experimental' so that the expected value
    in each case is 'experimental' — discriminating between "left alone" and
    "overwritten" and "silently set to stable".
    """
    import api.config as config
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    config.save_settings({"update_channel": "experimental"})
    result = config.save_settings(case["input"])
    # invalid/missing keys are ignored by save_settings; persisted value unchanged
    assert result.get("update_channel") == case["expected"], (
        f"normalization_case {case!r}: got {result.get('update_channel')!r}"
    )


# ---------------------------------------------------------------------------
# 5. Mode and state matrix
#    stale/current tab x unrelated/explicit change x stable/experimental/invalid/missing
# ---------------------------------------------------------------------------

# (tab_kind, change_kind, channel_value, initial_persisted, expected_persisted)
#
# tab_kind:
#   "stale"   = the selector holds channel_value while the persisted state is
#               initial_persisted (set by another tab); initial != channel_value
#               for unrelated rows — the core bug scenario.
#   "current" = selector matches initial_persisted.
#
# change_kind:
#   "unrelated" = generic autosave fires; payload has no update_channel.
#                 channel_value describes what the stale/current selector holds.
#   "explicit"  = user selects channel_value; raw value sent to server.
#
# channel_value for explicit rows: "invalid" = "nightly", "missing" = send {}.
# Server ignores invalid/missing -> persisted stays at initial_persisted.

_MATRIX = [
    # stale/unrelated — the primary bug scenario and its variants
    ("stale",   "unrelated", "experimental", "stable",       "stable"),       # BUG CASE: stale:exp, persisted:stable
    ("stale",   "unrelated", "stable",       "experimental", "experimental"), # stale:stable, persisted:exp
    ("stale",   "unrelated", "invalid",      "stable",       "stable"),       # stale selector holds invalid value
    ("stale",   "unrelated", "missing",      "stable",       "stable"),       # stale selector empty
    # current/unrelated
    ("current", "unrelated", "stable",       "stable",       "stable"),
    ("current", "unrelated", "experimental", "experimental", "experimental"),
    # current/explicit — user makes a deliberate channel selection
    ("current", "explicit",  "stable",       "experimental", "stable"),       # switch to stable
    ("current", "explicit",  "experimental", "stable",       "experimental"), # switch to experimental
    ("current", "explicit",  "invalid",      "experimental", "experimental"), # 'nightly' ignored -> stays experimental
    ("current", "explicit",  "missing",      "experimental", "experimental"), # {} -> no key -> stays experimental
]


@pytest.mark.parametrize(
    "tab_kind,change_kind,channel_value,initial_persisted,expected_persisted",
    _MATRIX,
    ids=[f"{t}-{c}-{v}" for t, c, v, _, _ in _MATRIX],
)
def test_mode_and_state_matrix(
    tmp_path,
    monkeypatch,
    tab_kind,
    change_kind,
    channel_value,
    initial_persisted,
    expected_persisted,
):
    """Full stale/current x unrelated/explicit x stable/experimental/invalid/missing matrix.

    For stale/unrelated rows, channel_value is the STALE selector value (different
    from initial_persisted). The generic autosave fires with no update_channel,
    so the persisted channel must stay at initial_persisted regardless.

    For explicit rows, channel_value is sent raw to save_settings; server-side
    validation rejects 'nightly' (invalid) and ignores absent keys (missing),
    leaving the channel at initial_persisted.
    """
    import api.config as config
    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)

    # Set the persisted state (what another tab or the previous state established).
    config.save_settings({"update_channel": initial_persisted})

    if change_kind == "unrelated":
        # For stale rows: channel_value is the stale selector value, which differs
        # from initial_persisted. Verify via node that the builder ignores it.
        if NODE is not None and tab_kind == "stale":
            payload = json.loads(
                _run_node(_preference_payload_script(PANELS_JS, channel_value or ""))
            )
            assert "update_channel" not in payload, (
                f"stale ({channel_value!r}) builder payload must not carry update_channel; "
                f"got {payload!r}"
            )
        # Fire the unrelated autosave (no update_channel).
        config.save_settings({"show_tps": True})

    elif change_kind == "explicit":
        if channel_value == "missing":
            config.save_settings({})
        elif channel_value == "invalid":
            config.save_settings({"update_channel": "nightly"})  # server ignores
        else:
            config.save_settings({"update_channel": channel_value})

    persisted = config.load_settings().get("update_channel", "stable")
    assert persisted == expected_persisted, (
        f"Matrix ({tab_kind},{change_kind},{channel_value!r}): "
        f"initial={initial_persisted!r}, expected {expected_persisted!r}, got {persisted!r}"
    )


# ---------------------------------------------------------------------------
# 6. Behavioral coverage for _saveUpdateChannelFromSelector edge cases
# ---------------------------------------------------------------------------

@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_channel_writer_null_selector():
    """Null or undefined selector must return without calling api."""
    script = _channel_writer_script_prelude(PANELS_JS) + """
(async () => {
  const calls = [];
  global.api = async function(url, opts) { calls.push({url, opts}); return {}; };
  await _saveUpdateChannelFromSelector(null);
  await _saveUpdateChannelFromSelector(undefined);
  console.log(JSON.stringify({ calls }));
})().catch(err => { console.error(err.message); process.exit(1); });
"""
    result = json.loads(_run_node(script))
    assert result["calls"] == [], (
        f"api must not be called for null/undefined selector; got {result['calls']!r}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_channel_writer_rejected_post():
    """Failed POST must revert the selector to the last confirmed server value
    (not the new value the user just picked, which is what the browser already
    applied to the <select> before the change event fires), sync the badge to
    match, and clear the status indicator without injecting a retry button.

    The stub models real listener semantics: _confirmedUpdateChannel='stable'
    (what the server last confirmed), sel.value='experimental' (user's new pick,
    already applied by the browser). After the failed POST the selector must
    show 'stable' again and the badge must be synced to 'stable'.
    """
    script = _channel_writer_script_prelude(PANELS_JS) + """
(async () => {
  const statusCalls = [];
  const badgeCalls = [];
  global._setPreferencesAutosaveStatus = function(s) { statusCalls.push(s); };
  global._syncUpdateChannelBadge = function(v) { badgeCalls.push(v); };
  global.api = async function() { throw new Error('network error'); };

  // Simulate real browser behavior: server last confirmed 'stable',
  // user picks 'experimental' -> browser applies it -> change event fires.
  global._confirmedUpdateChannel = 'stable';
  const sel = { value: 'experimental' };  // already the new value
  await _saveUpdateChannelFromSelector(sel);
  console.log(JSON.stringify({ selectorValue: sel.value, statusCalls, badgeCalls }));
})().catch(err => { console.error(err.message); process.exit(1); });
"""
    result = json.loads(_run_node(script))
    assert result["selectorValue"] == "stable", (
        f"selector must revert to _confirmedUpdateChannel ('stable') on failure; "
        f"got {result['selectorValue']!r}"
    )
    assert result["badgeCalls"] == ["stable"], (
        f"badge must be synced to 'stable' on failure; got {result['badgeCalls']!r}"
    )
    # Status sequence: 'saving' before POST, then null to clear on failure.
    # Must NOT contain 'failed' (its retry button replays the wrong payload).
    assert "saving" in result["statusCalls"], (
        f"'saving' status must be emitted before POST; got {result['statusCalls']!r}"
    )
    assert "failed" not in result["statusCalls"], (
        f"'failed' must not be used (retry button replays wrong payload); "
        f"got {result['statusCalls']!r}"
    )
    # null clears the 'saving' indicator
    assert None in result["statusCalls"], (
        f"null status must be emitted to clear the saving indicator on failure; "
        f"got {result['statusCalls']!r}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_channel_writer_undefined_response():
    """api() returning undefined on the 401 path must enter the failure branch."""
    script = _channel_writer_script_prelude(PANELS_JS) + """
(async () => {
  const statuses = [];
  const badges = [];
  global._setPreferencesAutosaveStatus = function(s) { statuses.push(s); };
  global._syncUpdateChannelBadge = function(v) { badges.push(v); };
  global.api = async function() { return undefined; };
  global._confirmedUpdateChannel = 'stable';
  const sel = { value: 'experimental' };
  await _saveUpdateChannelFromSelector(sel);
  console.log(JSON.stringify({ selectorValue: sel.value, confirmed: _confirmedUpdateChannel, statuses, badges }));
})().catch(err => { console.error(err.message); process.exit(1); });
"""
    result = json.loads(_run_node(script))
    assert result["selectorValue"] == "stable", (
        f"undefined api() response must revert to the confirmed value 'stable'; "
        f"got {result['selectorValue']!r}"
    )
    assert result["confirmed"] == "stable", (
        f"_confirmedUpdateChannel must remain 'stable'; got {result['confirmed']!r}"
    )
    assert result["badges"] == ["stable"]
    assert result["statuses"] == ["saving", None]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_channel_writer_string_response():
    """api() returning a string body on a non-JSON 200 must enter the failure branch."""
    script = _channel_writer_script_prelude(PANELS_JS) + """
(async () => {
  const statuses = [];
  const badges = [];
  global._setPreferencesAutosaveStatus = function(s) { statuses.push(s); };
  global._syncUpdateChannelBadge = function(v) { badges.push(v); };
  global.api = async function() { return '<html>login required</html>'; };
  global._confirmedUpdateChannel = 'stable';
  const sel = { value: 'experimental' };
  await _saveUpdateChannelFromSelector(sel);
  console.log(JSON.stringify({ selectorValue: sel.value, confirmed: _confirmedUpdateChannel, statuses, badges }));
})().catch(err => { console.error(err.message); process.exit(1); });
"""
    result = json.loads(_run_node(script))
    assert result["selectorValue"] == "stable", (
        f"string api() response must revert to the confirmed value 'stable'; "
        f"got {result['selectorValue']!r}"
    )
    assert result["confirmed"] == "stable", (
        f"_confirmedUpdateChannel must remain 'stable'; got {result['confirmed']!r}"
    )
    assert result["badges"] == ["stable"]
    assert result["statuses"] == ["saving", None]


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("response", [None, [], 7, False], ids=["null", "array", "number", "boolean"])
def test_channel_writer_non_object_response_reverts(response):
    """All JSON non-object response shapes are failed saves, not confirmations."""
    script = _channel_writer_script_prelude(PANELS_JS) + f"""
(async () => {{
  const statuses = [];
  global._setPreferencesAutosaveStatus = function(s) {{ statuses.push(s); }};
  global._syncUpdateChannelBadge = function() {{}};
  global.api = async function() {{ return {json.dumps(response)}; }};
  global._confirmedUpdateChannel = 'stable';
  const sel = {{ value: 'experimental' }};
  await _saveUpdateChannelFromSelector(sel);
  console.log(JSON.stringify({{ selectorValue: sel.value, confirmed: _confirmedUpdateChannel, statuses }}));
}})().catch(err => {{ console.error(err.message); process.exit(1); }});
"""
    result = json.loads(_run_node(script))
    assert result == {
        "selectorValue": "stable",
        "confirmed": "stable",
        "statuses": ["saving", None],
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_channel_writer_valid_object_missing_or_invalid_channel_falls_back_to_posted_value():
    """A valid JSON object with no usable channel preserves the server merge contract."""
    script = _channel_writer_script_prelude(PANELS_JS) + """
(async () => {
  const confirmed = [];
  let response = {};
  global._setPreferencesAutosaveStatus = function() {};
  global._syncUpdateChannelBadge = function(v) { confirmed.push(v); };
  global.api = async function() { return response; };
  const missing = { value: 'experimental' };
  global._confirmedUpdateChannel = 'stable';
  await _saveUpdateChannelFromSelector(missing);
  response = { update_channel: 'nightly' };
  const invalid = { value: 'experimental' };
  await _saveUpdateChannelFromSelector(invalid);
  console.log(JSON.stringify({ missing: missing.value, invalid: invalid.value, confirmedValue: _confirmedUpdateChannel, confirmed }));
})().catch(err => { console.error(err.message); process.exit(1); });
"""
    result = json.loads(_run_node(script))
    assert result == {
        "missing": "experimental",
        "invalid": "experimental",
        "confirmedValue": "experimental",
        "confirmed": ["experimental", "experimental"],
    }


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_status_slot_ownership():
    """Two writers share the single preferences autosave status slot.

    Scenario 1: generic autosave sets 'failed' with owner='preferences'.
    Channel write succeeds; its 'saved' call must be blocked — the 'failed'+Retry
    must stay visible so the user can retry the unsaved generic payload.

    Scenario 2: stale channel failure (superseded by a second write) fires a
    null-clear via the seq guard. That null-clear must NOT fire because
    seq!==_channelSaveSeq, so the generic autosave's 'failed' state is preserved.
    """
    script = _node_prelude(PANELS_JS) + """
global._preferencesAutosaveStatusOwner = null;

// DOM stub: tracks className and textContent/innerHTML separately.
function makeStatusEl() {
  return {
    _cls: '',
    _content: '',
    get className() { return this._cls; },
    set className(v) { this._cls = v; },
    classList: null,  // set below after object creation
    set textContent(v) { this._content = v; },
    get textContent() { return this._content; },
    set innerHTML(v) { this._content = v; },
  };
}
const statusEl = makeStatusEl();
statusEl.classList = {
  add(c) { statusEl._cls += (' ' + c); },
  contains(c) { return statusEl._cls.split(' ').includes(c); },
};
global.$ = function(id) {
  if (id === 'settingsPreferencesAutosaveStatus') return statusEl;
  return null;
};
global.t = function(k) { return k; };
global.esc = function(s) { return String(s); };

global._setPreferencesAutosaveStatus = (0, eval)(
  '(' + extractFunc(panelsSrc, '_setPreferencesAutosaveStatus') + ')'
);
global._saveUpdateChannelFromSelector = (0, eval)(
  '(' + extractFunc(panelsSrc, '_saveUpdateChannelFromSelector') + ')'
);
global._enqueueSettingsPost = (0, eval)(
  '(' + extractFunc(panelsSrc, '_enqueueSettingsPost') + ')'
);

(async () => {
  const results = {};

  // ── Scenario 1: generic autosave 'failed'; channel success must not overwrite ──
  _setPreferencesAutosaveStatus('failed', 'preferences');
  results.s1_afterGenericFailed = {
    cls: statusEl._cls.trim(),
    owner: _preferencesAutosaveStatusOwner,
  };

  // Channel write fires and succeeds
  global.api = async function() { return { update_channel: 'experimental' }; };
  global._confirmedUpdateChannel = 'stable';
  const sel1 = { value: 'experimental' };
  await _saveUpdateChannelFromSelector(sel1);
  results.s1_afterChannelSuccess = {
    cls: statusEl._cls.trim(),
    owner: _preferencesAutosaveStatusOwner,
    selectorValue: sel1.value,
  };

  // ── Scenario 2: stale channel failure's null-clear must not fire ──
  // Reset state for a clean scenario.
  statusEl._cls = '';
  statusEl._content = '';
  global._preferencesAutosaveStatusOwner = null;
  global._channelSaveSeq = 0;
  global._confirmedUpdateChannel = 'stable';

  // Generic autosave sets 'failed'.
  _setPreferencesAutosaveStatus('failed', 'preferences');
  results.s2_afterGenericFailed = { cls: statusEl._cls.trim() };

  // Channel write 1 starts (seq=1) but blocks.
  let resolveBlock;
  const block = new Promise(r => { resolveBlock = r; });
  let callCount = 0;
  global.api = async function(url, opts) {
    callCount++;
    if (callCount === 1) { await block; throw new Error('stale fail'); }
    return { update_channel: 'stable' };
  };

  const p1 = _saveUpdateChannelFromSelector({ value: 'experimental' });  // seq=1, blocks

  await Promise.resolve();
  // Channel write 2 is queued behind write 1; seq advances but api is not called.
  const sel2 = { value: 'stable' };
  const p2 = _saveUpdateChannelFromSelector(sel2);  // seq=2
  await Promise.resolve();
  results.s2_whileFirstBlocked = { cls: statusEl._cls.trim(), owner: _preferencesAutosaveStatusOwner, callCount };

  // Unblock write 1; its catch fires but seq(1)!==_channelSaveSeq(2) -> no null-clear.
  resolveBlock();
  await p1;
  await p2;
  results.s2_afterSecondWrite = { cls: statusEl._cls.trim(), owner: _preferencesAutosaveStatusOwner };
  results.s2_afterStaleFailure = { cls: statusEl._cls.trim(), owner: _preferencesAutosaveStatusOwner };

  console.log(JSON.stringify(results));
})().catch(err => { console.error(err.stack || err.message); process.exit(1); });
"""
    result = json.loads(_run_node(script))

    # Scenario 1
    assert "is-failed" in result["s1_afterGenericFailed"]["cls"], (
        f"generic 'failed' must be set; got {result['s1_afterGenericFailed']!r}"
    )
    assert result["s1_afterGenericFailed"]["owner"] == "preferences", (
        f"owner must be 'preferences'; got {result['s1_afterGenericFailed']['owner']!r}"
    )
    # Channel write succeeded but must NOT overwrite 'failed' from 'preferences'
    assert "is-failed" in result["s1_afterChannelSuccess"]["cls"], (
        f"channel 'saved' must be blocked by ownership guard; "
        f"cls is now {result['s1_afterChannelSuccess']['cls']!r}"
    )
    assert result["s1_afterChannelSuccess"]["selectorValue"] == "experimental", (
        f"selector must still update even when status write is blocked; "
        f"got {result['s1_afterChannelSuccess']['selectorValue']!r}"
    )

    # Scenario 2
    assert "is-failed" in result["s2_afterGenericFailed"]["cls"], (
        f"generic 'failed' must be set before channel write; got {result['s2_afterGenericFailed']!r}"
    )
    assert result["s2_whileFirstBlocked"]["callCount"] == 1, (
        f"queued write must not reach api before write 1 resolves; got {result['s2_whileFirstBlocked']!r}"
    )
    # After write 2 succeeds: write 2 tried to write 'saving' then 'saved', but
    # generic 'failed' blocks both → slot still shows 'failed'
    assert "is-failed" in result["s2_afterSecondWrite"]["cls"], (
        f"generic 'failed' must survive write 2; got {result['s2_afterSecondWrite']!r}"
    )
    # After stale write 1's failure: seq guard blocks the null-clear → slot unchanged
    assert "is-failed" in result["s2_afterStaleFailure"]["cls"], (
        f"stale failure's null-clear must be blocked by seq guard; "
        f"cls is now {result['s2_afterStaleFailure']['cls']!r}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_channel_writer_concurrent_selection(tmp_path, monkeypatch):
    """Two rapid selections serialize server writes and retain the latest UI value."""
    script = _channel_writer_script_prelude(PANELS_JS) + """
(async () => {
  const results = [];
  let resolveFirst;
  const firstCallBlocker = new Promise(resolve => { resolveFirst = resolve; });
  let apiCallCount = 0;
  const completed = [];

  global.api = async function(url, opts) {
    apiCallCount++;
    const body = JSON.parse(opts.body);
    if (apiCallCount === 1) await firstCallBlocker;
    completed.push(body);
    return { update_channel: body.update_channel };
  };

  const sel = { value: 'experimental' };
  global._confirmedUpdateChannel = null;
  const p1 = _saveUpdateChannelFromSelector(sel);  // seq=1, blocked
  sel.value = 'stable';                            // user changes selection again
  const p2 = _saveUpdateChannelFromSelector(sel);  // seq=2, queued

  await Promise.resolve();
  results.push({ while_first_blocked: sel.value, apiCallCount, completed });

  resolveFirst(null);  // unblock first call's response, then start the queued second call
  await Promise.all([p1, p2]);
  results.push({ after_both_complete: sel.value, apiCallCount, completed, confirmed: _confirmedUpdateChannel });

  console.log(JSON.stringify({ results, finalSeq: _channelSaveSeq }));
})().catch(err => { console.error(err.message); process.exit(1); });
"""
    result = json.loads(_run_node(script))
    results = result["results"]
    import api.config as config

    settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", settings_file)
    config.save_settings({"update_channel": "stable"})
    for body in results[1]["completed"]:
        config.save_settings(body)
    persisted = config.load_settings().get("update_channel")
    assert results[0]["while_first_blocked"] == "stable", (
        f"second selection must remain visible while first request is queued; got {results[0]!r}"
    )
    assert results[0]["apiCallCount"] == 1, (
        f"only the first settings POST may be in flight; got {results[0]!r}"
    )
    assert results[1]["after_both_complete"] == "stable", (
        f"after both serialized writes, selector must be 'stable'; "
        f"got {results[1]!r}"
    )
    assert results[1]["completed"] == [
        {"update_channel": "experimental"},
        {"update_channel": "stable"},
    ], results
    assert persisted == "stable", results
    assert results[1]["confirmed"] == "stable", results
    assert results[1]["apiCallCount"] == 2, results
    assert result["finalSeq"] == 2, (
        f"_channelSaveSeq must be 2 after two calls; got {result['finalSeq']!r}"
    )

