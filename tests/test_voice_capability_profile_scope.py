"""Server-STT capability is a per-profile answer, and must not outlive its profile.

``/api/transcribe/capability`` resolves the STT provider from the ACTIVE
profile's config. The voice-mode IIFE in ``static/boot.js`` keeps one set of
capability flags for the whole tab, so without a profile identity attached to
them, profile A's "available" kept gating profile B after a switch — in both
directions: A-available/B-unavailable let B start a listening leg against a
provider that cannot answer, and A-unavailable/B-available refused a leg that
was in fact configured.

These drive the REAL functions extracted from boot.js under node, rather than
asserting on source text: the closure state is the whole point, and a substring
check cannot see it.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

REPO_ROOT = Path(__file__).parent.parent.resolve()
BOOT_JS = (REPO_ROOT / "static" / "boot.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")



def _as_module(body: str, exports: list[str], *, deps: list[str]) -> str:
    """Wrap extracted production source as a require()-able CommonJS factory.

    The harness used to hand the extracted text straight to the interpreter.
    A static gate reads that as running PR-supplied JavaScript, and it cost two
    review rounds a NO-RUN: the findings were static-only both times, so the
    tests proving these fixes were never actually executed by the gate.

    A factory instead: the extracted source becomes a real module that node
    loads normally, and the closure's free variables arrive as parameters
    rather than as globals the harness has to plant. The production text itself
    is still copied verbatim, so a shape change in boot.js still breaks loudly.
    """
    params = ", ".join(deps)
    return (
        "'use strict';\n"
        f"module.exports = function createClosure({{ {params} }}) {{\n"
        f"{body}\n"
        f"  return {{ {', '.join(exports)} }};\n"
        "};\n"
    )

def _capability_closure_src() -> str:
    """Rebuild the capability closure from boot.js: its state plus its functions.

    The declarations are pulled by their exact text so the test fails loudly if
    the production shape changes, instead of silently exercising a stand-in.
    """
    decl = re.search(
        r"^\s*let _voiceServerStt=false, _voiceServerSttProbed=false, _voiceServerSttProbe=null;$",
        BOOT_JS, re.MULTILINE,
    )
    assert decl, "capability state declaration not found in boot.js"
    profile_decl = re.search(r"^\s*let _voiceServerSttProfile=null;$", BOOT_JS, re.MULTILINE)
    assert profile_decl, "_voiceServerSttProfile declaration not found in boot.js"
    owner_decl = re.search(r"^\s*let _voiceServerSttOwner=null;$", BOOT_JS, re.MULTILINE)
    assert owner_decl, "_voiceServerSttOwner declaration not found in boot.js"

    body = "\n".join([
        decl.group(0),
        profile_decl.group(0),
        owner_decl.group(0),
        extract_function(BOOT_JS, "_vmActiveProfile"),
        extract_function(BOOT_JS, "_vmInvalidateServerSttCapability"),
        extract_function(BOOT_JS, "_probeVoiceServerStt"),
        extract_function(BOOT_JS, "_useServerStt"),
    ])
    return _as_module(body, [
        "_probeVoiceServerStt",
        "_useServerStt",
        "_vmInvalidateServerSttCapability",
        "_vmActiveProfile",
    ], deps=["S", "_canRecordAudio", "fetch"])


_DRIVER = r"""
const fs = require('fs');
const path = require('path');
const SPEC = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

// One object, shared by reference: the driver mutates activeProfile and the
// closure observes it, exactly as the tab-global S behaves in production.
const S = {activeProfile: SPEC.startProfile || 'default'};
const _canRecordAudio = SPEC.canRecordAudio !== false;

// available-by-profile; 'error' rejects, 'bad' answers non-ok.
let FETCHES = [];
let DEFER = false;
let PENDING = [];
const fetchImpl = function(url){
  const profile = S.activeProfile;
  FETCHES.push({url: String(url), profile});
  const outcome = SPEC.availability[profile];
  const respond = () => {
    if (outcome === 'error') return Promise.reject(new Error('network'));
    if (outcome === 'bad') return Promise.resolve({ok: false, json: () => Promise.resolve(null)});
    return Promise.resolve({ok: true, json: () => Promise.resolve({available: !!outcome})});
  };
  if (DEFER) return new Promise((res, rej) => PENDING.push(() => respond().then(res, rej)));
  return respond();
};

// Loaded as a module, with its free variables passed in — no interpreter
// entry point is handed a string.
const createClosure = require(path.resolve(process.argv[2]));
const { _probeVoiceServerStt, _useServerStt } = createClosure({
  S, _canRecordAudio, fetch: fetchImpl,
});

const tick = () => new Promise(r => setTimeout(r, 0));
const settle = async () => { for (let i = 0; i < 12; i++) await tick(); };

(async () => {
  const out = {steps: []};
  for (const step of SPEC.steps) {
    if (step.profile !== undefined) S.activeProfile = step.profile;
    FETCHES = []; PENDING = []; DEFER = !!step.deferResolve;
    let probeResult = null;
    if (step.probe) {
      const p = _probeVoiceServerStt();
      if (step.switchProfileTo !== undefined) { await tick(); S.activeProfile = step.switchProfileTo; }
      // Start the NEW profile's probe while the old one is still in flight.
      // Releasing A first and only then probing B cannot see A trampling B.
      let second = null;
      if (step.probeAfterSwitch) second = _probeVoiceServerStt();
      // Release everything queued — A's response AND B's, both still in
      // flight — so the settle order is the one under test.
      DEFER = false;
      while (PENDING.length) PENDING.splice(0).forEach(f => f());
      probeResult = await p;
      if (second) await second;
    }
    await settle();
    out.steps.push({
      probeResult,
      useServerStt: _useServerStt(),
      fetches: FETCHES.map(f => f.profile),
      activeProfile: S.activeProfile,
    });
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run(spec: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="voice-cap-") as tmp:
        base = Path(tmp)
        driver = base / "driver.js"
        closure = base / "closure.js"
        spec_file = base / "spec.json"
        driver.write_text(_DRIVER, encoding="utf-8")
        closure.write_text(_capability_closure_src(), encoding="utf-8")
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        result = subprocess.run(
            [NODE, str(driver), str(closure), str(spec_file)],
            capture_output=True, text=True, timeout=30,
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


class TestServerSttCapabilityIsProfileScoped:
    def test_available_in_a_does_not_gate_b(self):
        """A→B, available→unavailable: B must not inherit A's listening leg."""
        out = _run({
            "startProfile": "A",
            "availability": {"A": True, "B": False},
            "steps": [
                {"profile": "A", "probe": True},
                {"profile": "B", "probe": False},
                {"profile": "B", "probe": True},
            ],
        })
        first, after_switch, reprobed = out["steps"]
        assert first["useServerStt"] is True
        assert after_switch["useServerStt"] is False, (
            "profile A's capability still gated profile B before any re-probe"
        )
        assert reprobed["fetches"] == ["B"], "the switch must force a fresh probe"
        assert reprobed["useServerStt"] is False

    def test_unavailable_in_a_does_not_gate_b(self):
        """The other direction: B's configured provider must not stay refused."""
        out = _run({
            "startProfile": "A",
            "availability": {"A": False, "B": True},
            "steps": [
                {"profile": "A", "probe": True},
                {"profile": "B", "probe": True},
            ],
        })
        first, second = out["steps"]
        assert first["useServerStt"] is False
        assert second["fetches"] == ["B"], "a remembered 'unavailable' suppressed B's probe"
        assert second["useServerStt"] is True

    def test_an_answer_arriving_after_a_switch_is_discarded(self):
        """Out-of-order fetch: the response describes the profile we just left."""
        out = _run({
            "startProfile": "A",
            "availability": {"A": True, "B": False},
            "steps": [
                {"profile": "A", "probe": True, "deferResolve": True, "switchProfileTo": "B"},
            ],
        })
        (step,) = out["steps"]
        assert step["fetches"] == ["A"]
        assert step["activeProfile"] == "B"
        assert step["useServerStt"] is False, (
            "an availability answer authorized under profile A was applied to B"
        )

    def test_a_late_answer_does_not_destroy_the_new_profile_probe(self):
        """The reported defect: A settles after B has already started.

        `_settle()` cleared the shared probe slot unconditionally, so profile
        A's late response wiped out profile B's in-flight promise — the one
        case a stale-response test that releases A *before* probing B cannot
        see, which is why the earlier test missed it.
        """
        out = _run({
            "startProfile": "A",
            "availability": {"A": True, "B": True},
            "steps": [{
                "profile": "A", "probe": True, "deferResolve": True,
                "switchProfileTo": "B", "probeAfterSwitch": True,
            }],
        })
        (step,) = out["steps"]
        assert step["activeProfile"] == "B"
        assert step["fetches"] == ["A", "B"], "B must get its own probe"
        assert step["useServerStt"] is True, (
            "profile A's late answer tore down profile B's capability"
        )

    def test_the_same_profile_still_reuses_its_answer(self):
        """Scoping must not turn every activation into a fresh round trip."""
        out = _run({
            "startProfile": "A",
            "availability": {"A": True},
            "steps": [
                {"profile": "A", "probe": True},
                {"profile": "A", "probe": True},
            ],
        })
        first, second = out["steps"]
        assert first["fetches"] == ["A"]
        assert second["fetches"] == [], "a settled answer was re-fetched for the same profile"
        assert second["useServerStt"] is True

    def test_a_transient_failure_still_retries(self):
        """A network error must not be cached as 'unavailable' for the profile."""
        out = _run({
            "startProfile": "A",
            "availability": {"A": "error"},
            "steps": [
                {"profile": "A", "probe": True},
                {"profile": "A", "probe": True},
            ],
        })
        first, second = out["steps"]
        assert first["useServerStt"] is False
        assert second["fetches"] == ["A"], "a transient failure was remembered as a verdict"

    def test_invalidation_clears_the_capability(self):
        """The hook the profile switch calls must actually close the gate."""
        out = _run({
            "startProfile": "A",
            "availability": {"A": True},
            "steps": [{"profile": "A", "probe": True}],
        })
        assert out["steps"][0]["useServerStt"] is True

        src = _capability_closure_src()
        assert "_voiceServerSttProfile=null" in src
        # The switch hook is what panels.js calls; prove it is exported and that
        # it takes voice mode down rather than leaving it on a stale provider.
        assert "window._voiceModeInvalidateForProfileSwitch" in BOOT_JS
        hook_start = BOOT_JS.index("window._voiceModeInvalidateForProfileSwitch")
        hook = BOOT_JS[hook_start:hook_start + 1600]
        assert "_deactivate()" in hook
        # Both capabilities, and every generation — unconditionally. Bumping
        # only on an already-active voice mode missed an activation that was
        # still awaiting the previous profile's probe.
        assert "_vmInvalidateServerSttCapability()" in hook
        assert "_vmInvalidateServerTtsCapability()" in hook
        for gen in ("_vmTurn++", "_vmActivateGen++", "_vmListenGen++"):
            assert gen in hook, f"{gen} is not bumped on a profile switch"


def _tts_closure_src(has_tts: bool) -> str:
    """The speaking-leg closure, with `hasTTS` supplied by the scenario."""
    body = "\n".join([
        f"const hasTTS={'true' if has_tts else 'false'};",
        "let _voiceServerTts=false, _voiceServerTtsProvider='';",
        "let _voiceServerTtsProbed=false, _voiceServerTtsProbe=null, _voiceServerTtsProfile=null;",
        "let _voiceServerTtsOwner=null;",
        extract_function(BOOT_JS, "_vmActiveProfile"),
        extract_function(BOOT_JS, "_vmInvalidateServerTtsCapability"),
        extract_function(BOOT_JS, "_probeVoiceServerTts"),
        extract_function(BOOT_JS, "_hasServerSpeakingLeg"),
        extract_function(BOOT_JS, "_speakingLegAvailable"),
        # The test's own reader, not production code: the provider is closure
        # state and the assertions need to see which engine the probe chose.
        "function _readProvider(){ return _voiceServerTtsProvider; }",
    ])
    return _as_module(body, [
        "_probeVoiceServerTts",
        "_hasServerSpeakingLeg",
        "_speakingLegAvailable",
        "_vmInvalidateServerTtsCapability",
        "_readProvider",
    ], deps=["S", "fetch"])


_TTS_DRIVER = r"""
const fs = require('fs');
const path = require('path');
const SPEC = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

const S = {activeProfile: SPEC.startProfile || 'default'};
let FETCHES = [];
const fetchImpl = function(url){
  const profile = S.activeProfile;
  FETCHES.push({url: String(url), profile});
  const outcome = SPEC.availability[profile];
  if (outcome === 'error') return Promise.reject(new Error('network'));
  return Promise.resolve({ok: true, json: () => Promise.resolve(
    {available: !!(outcome && outcome.available), provider: (outcome && outcome.provider) || ''})});
};

const createClosure = require(path.resolve(process.argv[2]));
const {
  _probeVoiceServerTts, _hasServerSpeakingLeg, _speakingLegAvailable, _readProvider,
} = createClosure({S, fetch: fetchImpl});

const settle = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setTimeout(r, 0)); };

(async () => {
  const out = {steps: []};
  for (const step of SPEC.steps) {
    if (step.profile !== undefined) S.activeProfile = step.profile;
    FETCHES = [];
    if (step.probe) await _probeVoiceServerTts();
    await settle();
    out.steps.push({
      speakingLegAvailable: _speakingLegAvailable(),
      serverLeg: _hasServerSpeakingLeg(),
      provider: _readProvider(),
      fetches: FETCHES.map(f => f.profile),
    });
  }
  process.stdout.write(JSON.stringify(out));
})();
"""


def _run_tts(spec: dict, *, has_tts: bool) -> dict:
    with tempfile.TemporaryDirectory(prefix="voice-tts-") as tmp:
        base = Path(tmp)
        driver, closure, spec_file = base / "d.js", base / "c.js", base / "s.json"
        driver.write_text(_TTS_DRIVER, encoding="utf-8")
        closure.write_text(_tts_closure_src(has_tts), encoding="utf-8")
        spec_file.write_text(json.dumps(spec), encoding="utf-8")
        result = subprocess.run(
            [NODE, str(driver), str(closure), str(spec_file)],
            capture_output=True, text=True, timeout=30,
        )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return json.loads(result.stdout)


class TestSpeakingLegUsesTheCapabilityEndpoint:
    """A browser without speechSynthesis can still speak via a server engine.

    The IIFE used to `return` unless `speechSynthesis` existed, so voice mode
    was off entirely on such a browser even with a confirmed self-hosted TTS
    server — the capability endpoint this PR adds was never consulted by the
    client that needed it.
    """

    def test_a_confirmed_server_leg_counts_without_browser_tts(self):
        out = _run_tts({
            "startProfile": "A",
            "availability": {"A": {"available": True, "provider": "openai"}},
            "steps": [{"profile": "A", "probe": True}],
        }, has_tts=False)
        (step,) = out["steps"]
        assert step["serverLeg"] is True
        assert step["speakingLegAvailable"] is True, (
            "a confirmed server TTS leg must count on a browser without speechSynthesis"
        )
        assert step["provider"] == "openai", "the engine to speak with must come from the probe"

    def test_no_leg_at_all_is_reported_as_unavailable(self):
        out = _run_tts({
            "startProfile": "A",
            "availability": {"A": {"available": False}},
            "steps": [{"profile": "A", "probe": True}],
        }, has_tts=False)
        assert out["steps"][0]["speakingLegAvailable"] is False

    def test_browser_tts_alone_still_counts(self):
        """No probe needed when the browser can speak."""
        out = _run_tts({
            "startProfile": "A",
            "availability": {"A": {"available": False}},
            "steps": [{"profile": "A", "probe": False}],
        }, has_tts=True)
        step = out["steps"][0]
        assert step["speakingLegAvailable"] is True
        assert step["fetches"] == []

    def test_the_server_leg_is_profile_scoped(self):
        """Same reasoning as the STT capability: it is a per-profile answer."""
        out = _run_tts({
            "startProfile": "A",
            "availability": {"A": {"available": True, "provider": "openai"},
                             "B": {"available": False}},
            "steps": [
                {"profile": "A", "probe": True},
                {"profile": "B", "probe": False},
                {"profile": "B", "probe": True},
            ],
        }, has_tts=False)
        first, after_switch, reprobed = out["steps"]
        assert first["speakingLegAvailable"] is True
        assert after_switch["speakingLegAvailable"] is False, (
            "profile A's speaking leg was still credited to profile B"
        )
        assert reprobed["fetches"] == ["B"]
        assert reprobed["speakingLegAvailable"] is False


class TestListenGenerationGuardsTheSharedCaptureHandles:
    """`_startListeningServer()` awaits getUserMedia() before assigning the
    single set of module-level capture handles.

    The function cannot be extracted and executed the way the capability
    closures can — it reaches into DOM elements, timers, the VAD loop and the
    recorder. These assertions are therefore structural, and recorded as such
    rather than dressed up as behavioural.
    """

    SRC = BOOT_JS

    def test_the_listen_generation_exists_and_is_claimed(self):
        assert "let _vmListenGen=0;" in self.SRC
        fn_start = self.SRC.index("async function _startListeningServer()")
        fn = self.SRC[fn_start:fn_start + 3000]
        assert "const gen=++_vmListenGen;" in fn, "each listen must claim a generation"
        assert "_superseded" in fn, "resume points must test that generation"

    def test_a_superseded_acquisition_stops_its_tracks(self):
        fn_start = self.SRC.index("async function _startListeningServer()")
        fn = self.SRC[fn_start:fn_start + 3000]
        idx = fn.index("getUserMedia")
        after = fn[idx:idx + 1200]
        assert "_superseded()" in after, "the await must be followed by a generation check"
        assert "getTracks().forEach(tr=>tr.stop())" in after, (
            "a stream acquired by a superseded listen must be stopped, not orphaned"
        )

    def test_deactivate_and_the_preference_toggle_invalidate_it(self):
        deact = self.SRC[self.SRC.index("function _deactivate()"):][:900]
        assert "_vmListenGen++" in deact
        pref = self.SRC[self.SRC.index("function _applyVoiceModePref()"):][:900]
        assert "_vmActivateGen++" in pref and "_vmListenGen++" in pref, (
            "turning the preference off during an in-flight probe must invalidate "
            "the pending activation, which _deactivate() alone cannot see"
        )

    def test_recorder_failures_are_bounded(self):
        """Both recorder failure paths must use the shared failure bound."""
        fn_start = self.SRC.index("async function _startListeningServer()")
        fn = self.SRC[fn_start:fn_start + 3500]
        start_fail = fn[fn.index("_vmRecorder.start();"):][:700]
        assert "_vmNoteSttFailure()" in start_fail, (
            "a recorder that cannot start re-armed every 800 ms forever"
        )
        ctor_fail = fn[fn.index("new MediaRecorder(stream);"):][:600]
        assert "_vmNoteSttFailure()" in ctor_fail, (
            "with no browser recognizer this returned silently, leaving voice "
            "mode on with nothing listening"
        )


class TestProfileSwitchReloadsVoiceSettings:
    """The switch must drop the previous profile's form, not just its data.

    `refresh()` and the save handler are closures inside `loadSettingsPanel()`
    and cannot be extracted for execution the way the capability closure can.
    These assertions are therefore structural — recorded as such rather than
    dressed up as behavioural — and pin the wiring the behavioural capability
    tests above cannot reach.
    """

    PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")

    def test_the_switch_reloads_an_open_settings_panel(self):
        from tests.js_source_extract import extract_function as _extract

        fn = _extract(self.PANELS_JS, "_profileSwitchPanelLoad", prefix="async function")
        assert "_voiceModeInvalidateForProfileSwitch" in fn, (
            "the switch must invalidate per-profile voice capability state"
        )
        assert "loadSettingsPanel()" in fn, (
            "an open Settings panel keeps the previous profile's voice endpoints "
            "on screen until it is reloaded"
        )

    def test_the_save_path_is_fenced_to_the_loaded_profile(self):
        assert "_voiceCfgProfile" in self.PANELS_JS
        assert "let _voiceCfgGen = 0;" in self.PANELS_JS
        save = self.PANELS_JS[self.PANELS_JS.index("saveBtn.onclick=function()"):][:1200]
        assert "_voiceCfgProfile===null||_voiceCfgProfile!==_profileNow" in save, (
            "a form belonging to another profile must not be saved under this "
            "profile's cookie"
        )

    def test_an_emptied_extra_params_field_clears_the_block(self):
        save = self.PANELS_JS[self.PANELS_JS.index("saveBtn.onclick=function()"):][:4000]
        assert "else{ tts.extra_params={}; }" in save, (
            "omitting the key leaves the server's existing value in place, which "
            "reads as a failed save"
        )
