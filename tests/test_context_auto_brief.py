# Auto end-of-turn brief regeneration (validated 2026-08-14): worker guards,
# canonical auxiliary routing, and route payload.
import json
import shutil
import subprocess
import sys
import time
from functools import lru_cache
from types import ModuleType, SimpleNamespace

import pytest

from api import context_brief as cb


@lru_cache(maxsize=1)
def _auto_refresh_harness():
    """Exercise the production auto-refresh callback under lifecycle races."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the auto-refresh lifecycle harness")
    src = open("static/panels.js").read()
    start = src.index("// Refresh visible brief panels when the background worker")
    end = src.index("\n// Banner shown", start)
    block = src[start:end]
    script = r"""
const vm = require('vm');
const timers = [];
const activeTimers = new Set();
const renders = [];
let clearCount = 0;
let requestCount = 0;
let currentSid = 'sid-a';
let currentPanels = [];
let apiImpl = null;
const requestPaths = [];

function deferred() {
  let resolve;
  const promise = new Promise(r => { resolve = r; });
  return {promise, resolve};
}
function makePanel(generation) {
  return {
    dataset: {briefSid: currentSid, briefLoaded: '1'},
    hidden: false,
    isConnected: true,
    offsetParent: {},
    _briefReqSeq: 4,
    _briefData: generation === null ? {} : {llm_brief: {generated_at: generation}},
  };
}
const ctx = {
  console,
  _loadSessionGeneration: 1,
  document: {querySelectorAll: () => currentPanels},
  _contextBriefSid: () => currentSid,
  api: (path, options) => {
    requestCount += 1;
    requestPaths.push(path);
    return apiImpl(path, options);
  },
  fetch: () => { throw new Error('auto refresh must use the subpath-aware api helper'); },
  renderContextBrief: (brief, panel) => {
    renders.push({sid: panel.dataset.briefSid, generation: brief.llm_brief.generated_at});
    panel._briefData = brief;
  },
  setInterval: callback => {
    const id = timers.length + 1;
    timers.push({id, callback});
    activeTimers.add(id);
    return id;
  },
  clearInterval: id => {
    clearCount += 1;
    activeTimers.delete(id);
  },
};
vm.createContext(ctx);
vm.runInContext(__BLOCK__ + `
this.syncAuto = _syncContextBriefAutoRefresh;
this.autoState = () => ({
  timer: _contextBriefAutoTimer,
  epoch: typeof _contextBriefAutoEpoch === 'undefined' ? null : _contextBriefAutoEpoch,
  inFlight: typeof _contextBriefAutoInFlightEpoch === 'undefined' ? null : _contextBriefAutoInFlightEpoch,
});`, ctx);

const latestTick = () => timers[timers.length - 1].callback();
const enabled = {enabled: true};
const disabled = {enabled: false};
const payload = (auto, generation) => ({
  ok: true,
  brief: {
    ...(auto === undefined ? {} : {auto}),
    llm_brief: {generated_at: generation},
  },
});

(async () => {
  const out = {};

  // A disabled/missing server flag must stop even when generation is unchanged.
  currentPanels = [makePanel(10)];
  ctx.syncAuto(enabled);
  let beforeRender = renders.length;
  let beforeClear = clearCount;
  apiImpl = async () => payload(disabled, 10);
  await latestTick();
  out.unchangedDisable = {
    activeTimers: activeTimers.size,
    clears: clearCount - beforeClear,
    renders: renders.length - beforeRender,
  };
  ctx.syncAuto(enabled);
  beforeRender = renders.length;
  beforeClear = clearCount;
  apiImpl = async () => payload(undefined, 11);
  await latestTick();
  out.missingDisable = {
    activeTimers: activeTimers.size,
    clears: clearCount - beforeClear,
    renders: renders.length - beforeRender,
  };

  // A transient API failure must not be confused with an authoritative disable.
  ctx.syncAuto(enabled);
  beforeRender = renders.length;
  apiImpl = async () => { throw new Error('transient'); };
  await latestTick();
  out.transientError = {
    activeTimers: activeTimers.size,
    renders: renders.length - beforeRender,
  };
  out.requestPaths = requestPaths.slice();

  // Stop while the API request is pending: the old owner cannot continue afterward.
  ctx.syncAuto(enabled);
  const pendingStop = deferred();
  apiImpl = () => pendingStop.promise;
  beforeRender = renders.length;
  const stoppedPoll = latestTick();
  await Promise.resolve();
  ctx.syncAuto(disabled);
  pendingStop.resolve(payload(enabled, 20));
  await stoppedPoll;
  out.disableDuringFetch = {
    activeTimers: activeTimers.size,
    renders: renders.length - beforeRender,
  };

  // Session and load-generation ownership are both revalidated after await.
  currentSid = 'sid-a';
  currentPanels = [makePanel(10)];
  ctx.syncAuto(enabled);
  const pendingSession = deferred();
  apiImpl = () => pendingSession.promise;
  beforeRender = renders.length;
  const sessionPoll = latestTick();
  await Promise.resolve();
  currentSid = 'sid-b';
  pendingSession.resolve(payload(enabled, 20));
  await sessionPoll;
  out.sessionChange = renders.length - beforeRender;
  ctx.syncAuto(disabled);

  currentSid = 'sid-a';
  currentPanels = [makePanel(10)];
  ctx.syncAuto(enabled);
  const pendingGeneration = deferred();
  apiImpl = () => pendingGeneration.promise;
  beforeRender = renders.length;
  const generationPoll = latestTick();
  await Promise.resolve();
  ctx._loadSessionGeneration += 1;
  pendingGeneration.resolve(payload(enabled, 20));
  await generationPoll;
  out.loadGenerationChange = renders.length - beforeRender;
  ctx.syncAuto(disabled);

  currentPanels = [makePanel(10)];
  ctx.syncAuto(enabled);
  const pendingPanelSeq = deferred();
  apiImpl = () => pendingPanelSeq.promise;
  beforeRender = renders.length;
  const panelSeqPoll = latestTick();
  await Promise.resolve();
  currentPanels[0]._briefReqSeq += 1;
  pendingPanelSeq.resolve(payload(enabled, 20));
  await panelSeqPoll;
  out.panelRequestSequenceChange = renders.length - beforeRender;
  ctx.syncAuto(disabled);

  currentPanels = [makePanel(10)];
  ctx.syncAuto(enabled);
  const pendingPanelSid = deferred();
  apiImpl = () => pendingPanelSid.promise;
  beforeRender = renders.length;
  const panelSidPoll = latestTick();
  await Promise.resolve();
  currentPanels[0].dataset.briefSid = 'sid-b';
  pendingPanelSid.resolve(payload(enabled, 20));
  await panelSidPoll;
  out.panelSidChange = renders.length - beforeRender;
  ctx.syncAuto(disabled);

  currentPanels = [makePanel(10)];
  ctx.syncAuto(enabled);
  const pendingVisibility = deferred();
  apiImpl = () => pendingVisibility.promise;
  beforeRender = renders.length;
  const visibilityPoll = latestTick();
  await Promise.resolve();
  currentPanels[0].offsetParent = null;
  pendingVisibility.resolve(payload(enabled, 20));
  await visibilityPoll;
  out.panelVisibilityChange = renders.length - beforeRender;
  ctx.syncAuto(disabled);

  // A manual response can install generation 30 while auto generation 20 parses.
  currentPanels = [makePanel(10)];
  ctx.syncAuto(enabled);
  const pendingAuto = deferred();
  apiImpl = () => pendingAuto.promise;
  beforeRender = renders.length;
  const olderAutoPoll = latestTick();
  await Promise.resolve();
  currentPanels[0]._briefData = {llm_brief: {generated_at: 30}};
  pendingAuto.resolve(payload(enabled, 20));
  await olderAutoPoll;
  out.manualNewerAutoOlder = renders.length - beforeRender;
  ctx.syncAuto(disabled);

  // Two interval ticks under one owner serialize to one network request.
  currentPanels = [makePanel(30)];
  ctx.syncAuto(enabled);
  const overlapping = deferred();
  apiImpl = () => overlapping.promise;
  const requestBefore = requestCount;
  beforeRender = renders.length;
  const firstTick = latestTick();
  await Promise.resolve();
  const secondTick = latestTick();
  await Promise.resolve();
  overlapping.resolve(payload(enabled, 40));
  await Promise.all([firstTick, secondTick]);
  out.overlappingTicks = {
    fetches: requestCount - requestBefore,
    renders: renders.length - beforeRender,
  };
  ctx.syncAuto(disabled);

  // A stopped interval's callback stays stale after a fresh owner is enabled.
  currentPanels = [makePanel(40)];
  ctx.syncAuto(enabled);
  const staleCallback = timers[timers.length - 1].callback;
  ctx.syncAuto(disabled);
  ctx.syncAuto(enabled);
  const freshCallback = timers[timers.length - 1].callback;
  apiImpl = async () => payload(enabled, 50);
  const restartRequestBefore = requestCount;
  beforeRender = renders.length;
  await staleCallback();
  await freshCallback();
  out.stopReenable = {
    activeTimers: activeTimers.size,
    fetches: requestCount - restartRequestBefore,
    renders: renders.length - beforeRender,
  };
  ctx.syncAuto(disabled);

  console.log(JSON.stringify(out));
})().catch(error => {
  console.error(error && error.stack || error);
  process.exit(1);
});
""".replace("__BLOCK__", json.dumps(block))
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


def _autosave_auto_sync_harness():
    """Run the production preferences autosave function with server-confirmed values."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node is required for the preferences autosave harness")
    src = open("static/panels.js").read()
    start = src.index("async function _autosavePreferencesSettings(payload){")
    end = src.index("\nfunction _retryPreferencesAutosave", start)
    function_src = src[start:end]
    script = r"""
const vm = require('vm');
const syncCalls = [];
const saved = [{context_brief_auto: false}, {context_brief_auto: true}];
const ctx = {
  console,
  window: {},
  document: {documentElement: {dataset: {}}},
  $: () => null,
  _enqueueSettingsPost: async () => saved.shift(),
  _syncContextBriefAutoRefresh: auto => syncCalls.push(auto && auto.enabled),
  _setPreferencesAutosaveStatus: () => {},
  _settingsPreferencesAutosaveRetryPayload: null,
  _settingsDirty: false,
  _settingsHermesDefaultModelOnOpen: '',
  _settingsHermesDefaultModelProviderOnOpen: null,
};
vm.createContext(ctx);
vm.runInContext(__FUNCTION__ + '\nthis.savePreferences = _autosavePreferencesSettings;', ctx);
(async () => {
  await ctx.savePreferences({context_brief_auto: false});
  await ctx.savePreferences({context_brief_auto: true});
  console.log(JSON.stringify(syncCalls));
})().catch(error => {
  console.error(error && error.stack || error);
  process.exit(1);
});
""".replace("__FUNCTION__", json.dumps(function_src))
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip())


@pytest.fixture(autouse=True)
def _clean_auto_state(monkeypatch, tmp_path):
    monkeypatch.setattr(cb, "_AUTO_LAST_ENQUEUE_AT", {})
    monkeypatch.setattr(cb, "_AUTO_PENDING_SESSION_IDS", set(), raising=False)
    with cb._JOBS_LOCK:
        cb._JOBS.clear()
    yield


def _session(sid="20260814_000000_abc123", n_messages=5):
    """Attribute-access session object, as returned by _resolve_session."""
    return SimpleNamespace(
        id=sid,
        archived=False,
        updated_at=200.0,
        messages=[{"role": "user", "content": f"m{i}"} for i in range(n_messages)],
    )


def _listed(sid, n_messages=5):
    """Metadata dict as returned by all_sessions() (no messages loaded)."""
    return {"session_id": sid, "message_count": n_messages, "updated_at": time.time()}


class TestAutoConfig:
    def test_defaults(self, monkeypatch):
        monkeypatch.setattr("api.config.load_settings", lambda: {})
        cfg = cb.get_auto_config()
        # Direction decision 2026-08-15: auto brief is opt-in via the Settings
        # switch; default must be off. Manual ↻ regeneration always works.
        assert cfg["enabled"] is False
        assert cfg["min_interval_seconds"] == 60.0
        assert "model" not in cfg
        assert "effort" not in cfg
        assert "choices" not in cfg

    def test_legacy_model_setting_is_ignored(self, monkeypatch):
        monkeypatch.setattr(
            "api.config.load_settings", lambda: {"context_brief_model": "gpt-5.6-luna"}
        )
        cfg = cb.get_auto_config()
        assert "model" not in cfg
        assert "choices" not in cfg

    def test_interval_clamped(self, monkeypatch):
        monkeypatch.setattr(
            "api.config.load_settings",
            lambda: {"context_brief_min_interval_seconds": 5, "context_brief_auto": False},
        )
        cfg = cb.get_auto_config()
        assert cfg["min_interval_seconds"] == 30.0
        assert cfg["enabled"] is False


class TestAuxiliaryRouting:
    def _fake_call_llm(self, monkeypatch, captured):
        def fake(**kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "x" * 300}}]}

        agent_module = ModuleType("agent")
        agent_module.__path__ = []
        auxiliary_module = ModuleType("agent.auxiliary_client")
        auxiliary_module.call_llm = fake
        agent_module.auxiliary_client = auxiliary_module
        monkeypatch.setitem(sys.modules, "agent", agent_module)
        monkeypatch.setitem(sys.modules, "agent.auxiliary_client", auxiliary_module)
        monkeypatch.setattr(cb, "_distill_context_brief", lambda s: "distilled")
        monkeypatch.setattr(cb, "_extract_llm_content", lambda r: "x" * 300)

    def test_generate_uses_compression_slot_without_model_override(self, monkeypatch):
        captured = {}
        self._fake_call_llm(monkeypatch, captured)
        text, source = cb._generate_llm_brief(
            _session(), "sid", {"meta": {"title": "t", "message_count": 5}}
        )
        assert source == "auxiliary-llm"
        assert captured["task"] == "compression"
        assert "model" not in captured
        assert "reasoning_config" not in captured


class TestWorkerGuards:
    def _arm(self, monkeypatch, sessions, stored=None, active_sids=(), enabled=True):
        monkeypatch.setattr("api.config.load_settings", lambda: {"context_brief_auto": enabled})
        monkeypatch.setattr(cb, "load_llm_brief", lambda s, sid: (stored or {}).get(sid))

        import api.config as cfg_mod

        resolved = {
            str(meta.get("session_id") or meta.get("id")): _session(
                str(meta.get("session_id") or meta.get("id")),
                int(meta.get("message_count") or 0),
            )
            for meta in sessions
        }
        monkeypatch.setattr(
            cfg_mod,
            "claim_finished_run_session_ids",
            lambda: set(resolved),
        )
        monkeypatch.setattr(cb, "_resolve_session", lambda sid: (resolved[sid], "webui"))
        monkeypatch.setattr(
            "api.models.all_sessions",
            lambda **kw: (_ for _ in ()).throw(AssertionError("global scan forbidden")),
        )
        monkeypatch.setattr(
            cfg_mod, "ACTIVE_RUNS", {f"st-{sid}": {"session_id": sid} for sid in active_sids}
        )
        started = []
        monkeypatch.setattr(
            cb,
            "start_brief_job",
            lambda sid, **_kwargs: started.append(sid) or {"job_id": "j"},
        )
        return started

    def test_disabled_mode_discards_finished_and_pending_without_replay(
        self, monkeypatch
    ):
        import api.config as cfg_mod

        enabled = {"value": False}
        finished_batches = [
            {f"disabled-{index}" for index in range(100)},
            set(),
        ]
        started = []
        monkeypatch.setattr(
            "api.config.load_settings",
            lambda: {"context_brief_auto": enabled["value"]},
        )
        monkeypatch.setattr(
            cfg_mod,
            "claim_finished_run_session_ids",
            lambda: finished_batches.pop(0),
        )
        monkeypatch.setattr(
            cb,
            "start_brief_job",
            lambda sid, **_kwargs: started.append(sid) or {"job_id": "j"},
        )
        cb._add_auto_pending({"old-pending"})
        cb._AUTO_LAST_ENQUEUE_AT["old-pending"] = 1.0

        cb._auto_tick()

        assert cb._auto_pending_snapshot() == ()
        assert cb._AUTO_LAST_ENQUEUE_AT == {}
        assert started == []

        enabled["value"] = True
        cb._auto_tick()

        assert cb._auto_pending_snapshot() == ()
        assert started == []

    def test_enqueues_only_stale_idle_sessions(self, monkeypatch):
        fresh = _listed("s_fresh", 5)
        stale = _listed("s_stale", 7)
        busy = _listed("s_busy", 3)
        stored = {
            "s_fresh": {
                "message_count_at_generation": 5,
                "transcript_revision_at_generation": cb._transcript_revision(
                    _session("s_fresh", 5)
                ),
            },
            "s_stale": {"message_count_at_generation": 4},   # moved → regenerate
            "s_busy": None,                                   # never generated → would enqueue
        }
        started = self._arm(
            monkeypatch, [fresh, stale, busy], stored=stored, active_sids=("s_busy",)
        )
        cb._auto_tick()
        assert started == ["s_stale"]  # busy excluded by active-run guard

    def test_no_scan_without_turn_end(self, monkeypatch):
        started = self._arm(monkeypatch, [_listed("s0")])
        import api.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "claim_finished_run_session_ids", lambda: set())
        cb._auto_tick()
        assert started == []

    def test_min_interval_debounce(self, monkeypatch):
        s = _listed("s1", 5)
        started = self._arm(monkeypatch, [s], stored={"s1": {"message_count_at_generation": 1}})
        cb._auto_tick()
        cb._auto_tick()  # a duplicate finish event remains inside the debounce window
        assert started == ["s1"]
        cb._auto_tick()
        assert started == ["s1"]

    def test_same_count_newer_session_revision_is_enqueued(self, monkeypatch):
        s = _listed("s_revision", 5)
        started = self._arm(
            monkeypatch,
            [s],
            stored={
                "s_revision": {
                    "message_count_at_generation": 5,
                    "session_updated_at_at_generation": 100.0,
                }
            },
        )

        cb._auto_tick()

        assert started == ["s_revision"]

    def test_burst_cap(self, monkeypatch):
        sessions = [_listed(f"s{i}", 5) for i in range(5)]
        started = self._arm(monkeypatch, sessions, stored={})
        cb._auto_tick()
        assert len(started) == cb._AUTO_MAX_PER_TICK

    def test_disabled(self, monkeypatch):
        started = self._arm(monkeypatch, [_listed("s0")], stored={}, enabled=False)
        cb._auto_tick()
        assert started == []

    def test_targets_only_finished_session_without_global_scan(self, monkeypatch):
        import api.config as cfg_mod

        monkeypatch.setattr("api.config.load_settings", lambda: {"context_brief_auto": True})
        monkeypatch.setattr(
            cfg_mod,
            "claim_finished_run_session_ids",
            lambda: {"s_changed"},
            raising=False,
        )
        monkeypatch.setattr(cfg_mod, "LAST_RUN_FINISHED_AT", time.time() + 100)
        monkeypatch.setattr(
            "api.models.all_sessions",
            lambda **kw: (_ for _ in ()).throw(AssertionError("global scan forbidden")),
        )
        monkeypatch.setattr(cb, "_resolve_session", lambda sid: (_session(sid, 7), "webui"))
        monkeypatch.setattr(
            cb,
            "load_llm_brief",
            lambda session, sid: {"message_count_at_generation": 4},
        )
        monkeypatch.setattr(cfg_mod, "ACTIVE_RUNS", {})
        started = []
        monkeypatch.setattr(
            cb,
            "start_brief_job",
            lambda sid, **_kwargs: started.append(sid) or {"job_id": "j"},
        )

        cb._auto_tick()

        assert started == ["s_changed"]

    def test_archived_finished_session_is_never_auto_enqueued(self, monkeypatch):
        import api.config as cfg_mod

        archived = _session("s_archived", 7)
        archived.archived = True
        monkeypatch.setattr("api.config.load_settings", lambda: {"context_brief_auto": True})
        monkeypatch.setattr(
            cfg_mod,
            "claim_finished_run_session_ids",
            lambda: {"s_archived"},
            raising=False,
        )
        monkeypatch.setattr(cfg_mod, "LAST_RUN_FINISHED_AT", time.time() + 100)
        monkeypatch.setattr("api.models.all_sessions", lambda **kw: [_listed("s_archived", 7)])
        monkeypatch.setattr(cb, "_resolve_session", lambda sid: (archived, "webui"))
        monkeypatch.setattr(
            cb,
            "load_llm_brief",
            lambda session, sid: {"message_count_at_generation": 4},
        )
        monkeypatch.setattr(cfg_mod, "ACTIVE_RUNS", {})
        started = []
        monkeypatch.setattr(
            cb,
            "start_brief_job",
            lambda sid, **_kwargs: started.append(sid) or {"job_id": "j"},
        )

        cb._auto_tick()

        assert started == []

    def test_running_older_job_keeps_new_finish_event_pending(self, monkeypatch):
        sid = "s_running"
        started = self._arm(monkeypatch, [_listed(sid, 5)], stored={})
        with cb._JOBS_LOCK:
            cb._JOBS["old-job"] = {"session_id": sid, "status": "running"}

        cb._auto_tick()

        assert started == []
        assert sid in cb._AUTO_PENDING_SESSION_IDS

    def test_requeue_during_start_survives_pending_claim(self, monkeypatch):
        sid = "s_requeued_during_start"
        self._arm(monkeypatch, [_listed(sid, 5)], stored={})

        def start_and_requeue(_sid, **_kwargs):
            cb._requeue_auto_session(_sid)
            return {"job_id": "j"}

        monkeypatch.setattr(cb, "start_brief_job", start_and_requeue)

        cb._auto_tick()

        assert sid in cb._AUTO_PENDING_SESSION_IDS

    def test_successor_admission_fields_block_worker_without_registry(self, monkeypatch):
        import api.config as cfg_mod

        sid = "s_admitted_before_worker"
        session = _session(sid, 5)
        session.active_stream_id = "new-stream"
        session.pending_user_message = "new user turn"
        monkeypatch.setattr(cfg_mod, "ACTIVE_RUNS", {})
        monkeypatch.setattr(cb, "_resolve_session", lambda _sid: (session, "webui"))
        generated = []
        monkeypatch.setattr(
            cb,
            "_generate_llm_brief",
            lambda *_args, **_kwargs: generated.append(True) or ("x" * 300, "test"),
        )
        job = {
            "job_id": "j-admission",
            "session_id": sid,
            "_automatic": True,
            "_generation": cb._SID_GENERATIONS.get(sid, 0),
            "status": "running",
        }

        cb._run_brief_job(job)

        assert generated == []
        assert job["status"] == "error"
        assert "resumed before" in job["error"]
        assert sid in cb._AUTO_PENDING_SESSION_IDS

    def test_successor_admitted_after_model_blocks_persistence(
        self, monkeypatch
    ):
        sid = "auto-admission-before-save"
        session = _session(sid, 1)
        saved = []

        monkeypatch.setattr(cb, "_resolve_session", lambda requested: (session, "webui"))
        monkeypatch.setattr(
            cb,
            "build_deterministic_brief",
            lambda *_args, **_kwargs: {"meta": {"message_count": 1}},
        )

        def admit_then_return(*_args, **_kwargs):
            session.active_stream_id = "successor-stream"
            session.pending_user_message = "next turn"
            return "brief", "llm"

        monkeypatch.setattr(cb, "_generate_llm_brief", admit_then_return)
        monkeypatch.setattr(
            cb,
            "_save_llm_brief",
            lambda *_args, **_kwargs: saved.append(True),
        )
        job = {
            "session_id": sid,
            "_automatic": True,
            "_generation": cb._SID_GENERATIONS.get(sid, 0),
        }

        cb._run_brief_job(job)

        assert job["status"] == "error"
        assert "resumed" in job["error"]
        assert saved == []
        assert sid in cb._auto_pending_snapshot()

    def test_deleted_finished_session_is_dropped(self, monkeypatch):
        import api.config as cfg_mod

        monkeypatch.setattr("api.config.load_settings", lambda: {"context_brief_auto": True})
        monkeypatch.setattr(cfg_mod, "claim_finished_run_session_ids", lambda: {"gone"})
        monkeypatch.setattr(cfg_mod, "ACTIVE_RUNS", {})
        monkeypatch.setattr(
            cb,
            "_resolve_session",
            lambda _sid: (_ for _ in ()).throw(cb.BriefError("Session not found", 404)),
        )

        cb._auto_tick()

        assert "gone" not in cb._AUTO_PENDING_SESSION_IDS

    def test_thread_start_failure_removes_running_job(self, monkeypatch):
        session = _session("s_thread_start_failure", 5)
        monkeypatch.setattr(cb, "_resolve_session", lambda _sid: (session, "webui"))

        def fail_start(_thread):
            raise RuntimeError("synthetic thread start failure")

        monkeypatch.setattr(cb.threading.Thread, "start", fail_start)

        with pytest.raises(RuntimeError, match="synthetic thread start failure"):
            cb.start_brief_job(session.id)
        with cb._JOBS_LOCK:
            assert not any(
                job.get("session_id") == session.id and job.get("status") == "running"
                for job in cb._JOBS.values()
            )

    def test_finished_run_session_ids_are_claimed_once(self, monkeypatch):
        import api.config as cfg_mod

        monkeypatch.setattr(cfg_mod, "ACTIVE_RUNS", {})
        monkeypatch.setattr(cfg_mod, "FINISHED_RUN_SESSION_IDS", set(), raising=False)
        cfg_mod.register_active_run("st-1", session_id="s1")
        cfg_mod.register_active_run("st-2", session_id="s2")
        cfg_mod.unregister_active_run("st-1")
        cfg_mod.unregister_active_run("st-2")

        assert cfg_mod.claim_finished_run_session_ids() == {"s1", "s2"}
        assert cfg_mod.claim_finished_run_session_ids() == set()

    def test_saved_brief_records_session_revision(self, monkeypatch, tmp_path):
        session = _session("s_saved", 3)
        path = tmp_path / "brief.json"
        monkeypatch.setattr(cb, "_brief_store_path", lambda _session, _sid: path)

        payload = cb._save_llm_brief(
            session,
            session.id,
            text="brief",
            source="test",
            message_count=3,
        )

        assert payload is not None
        assert payload["session_updated_at_at_generation"] == session.updated_at
        assert payload["transcript_revision_at_generation"]

        session.messages[0]["content"] = "same count, rewritten content"
        assert cb._llm_brief_is_current(session, payload, 3) is False

    def test_job_does_not_persist_stale_snapshot_and_requeues_auto(self, monkeypatch):
        sid = "s_changed_during_llm"
        initial = _session(sid, 3)
        fresh = _session(sid, 3)
        fresh.messages[0]["content"] = "rewritten while LLM was running"
        resolutions = iter(((initial, "webui"), (fresh, "webui")))
        monkeypatch.setattr(cb, "_resolve_session", lambda _sid: next(resolutions))
        monkeypatch.setattr(
            cb,
            "build_deterministic_brief",
            lambda session, _sid, source: {"meta": {"message_count": len(session.messages)}},
        )
        monkeypatch.setattr(cb, "_generate_llm_brief", lambda *_args, **_kwargs: ("x" * 300, "test"))
        saved = []
        monkeypatch.setattr(cb, "_save_llm_brief", lambda *_args, **_kwargs: saved.append(True))
        job = {
            "session_id": sid,
            "status": "running",
            "_automatic": True,
            "_generation": cb._SID_GENERATIONS.get(sid, 0),
        }

        cb._run_brief_job(job)

        assert saved == []
        assert job["status"] == "error"
        assert sid in cb._AUTO_PENDING_SESSION_IDS

    def test_job_does_not_persist_after_archive_race(self, monkeypatch):
        sid = "s_archived_during_llm"
        initial = _session(sid, 3)
        archived = _session(sid, 3)
        archived.archived = True
        resolutions = iter(((initial, "webui"), (archived, "webui")))
        monkeypatch.setattr(cb, "_resolve_session", lambda _sid: next(resolutions))
        monkeypatch.setattr(
            cb,
            "build_deterministic_brief",
            lambda session, _sid, source: {"meta": {"message_count": len(session.messages)}},
        )
        monkeypatch.setattr(cb, "_generate_llm_brief", lambda *_args, **_kwargs: ("x" * 300, "test"))
        saved = []
        monkeypatch.setattr(cb, "_save_llm_brief", lambda *_args, **_kwargs: saved.append(True))
        job = {
            "session_id": sid,
            "status": "running",
            "_automatic": True,
            "_generation": cb._SID_GENERATIONS.get(sid, 0),
        }

        cb._run_brief_job(job)

        assert saved == []
        assert job["status"] == "error"
        assert sid not in cb._AUTO_PENDING_SESSION_IDS

    def test_worker_lifecycle(self):
        assert cb.start_auto_brief_worker() is True
        assert cb.start_auto_brief_worker() is False  # idempotent
        assert cb.stop_auto_brief_worker() is True


class TestRoutePayload:
    def test_brief_payload_carries_auto_block(self):
        import inspect

        import api.routes as routes

        src = inspect.getsource(routes)
        idx = src.index('"/api/session/context-brief"')
        window = src[idx : idx + 1200]
        assert 'brief["auto"] = get_auto_config()' in window

    def test_server_starts_worker(self):
        src = open("server.py").read()
        assert "start_auto_brief_worker()" in src


class TestFrontendStatic:
    def test_auto_refresh_present_without_redundant_model_select(self):
        src = open("static/panels.js").read()
        assert "_contextBriefAutoTimer = setInterval" in src
        assert "_contextBriefModelSelect" not in src
        assert "_contextBriefModelChange" not in src
        assert "context_brief_model" not in src
        assert ".ctx-brief-model-select" not in open("static/style.css").read()

    def test_refresh_reads_match_payload_key(self):
        # The auto-refresh generation helper must read brief.llm_brief (the only
        # key the server emits) before rendering renderContextBrief(brief, panel).
        src = open("static/panels.js").read()
        assert "const llm = brief && brief.llm_brief;" in src
        assert "data.brief.llm)" not in src
        assert "renderContextBrief(data.brief, item.panel)" in src
        assert "renderContextBrief(item.panel, data.brief)" not in src

    def test_settings_keys_registered(self):
        src = open("api/config.py").read()
        for key in ('"context_brief_auto"', '"context_brief_min_interval_seconds"'):
            assert key in src, key
        assert '"context_brief_model"' not in src

    def test_default_auto_off_in_settings_defaults(self):
        src = open("api/config.py").read()
        assert '"context_brief_auto": False' in src

    def test_auto_refresh_stops_on_unchanged_generation_disable(self):
        result = _auto_refresh_harness()
        assert result["unchangedDisable"] == {"activeTimers": 0, "clears": 1, "renders": 0}
        assert result["missingDisable"] == {"activeTimers": 0, "clears": 1, "renders": 0}

    def test_auto_refresh_uses_subpath_aware_api_and_survives_transient_errors(self):
        result = _auto_refresh_harness()
        assert result["transientError"] == {"activeTimers": 1, "renders": 0}
        assert result["requestPaths"]
        assert set(result["requestPaths"]) == {"/api/session/context-brief"}

    def test_auto_refresh_disable_during_fetch_invalidates_owner(self):
        assert _auto_refresh_harness()["disableDuringFetch"] == {
            "activeTimers": 0,
            "renders": 0,
        }

    def test_auto_refresh_rejects_session_and_load_generation_changes(self):
        result = _auto_refresh_harness()
        assert result["sessionChange"] == 0
        assert result["loadGenerationChange"] == 0

    def test_auto_refresh_rejects_panel_owner_changes_after_await(self):
        result = _auto_refresh_harness()
        assert result["panelRequestSequenceChange"] == 0
        assert result["panelSidChange"] == 0
        assert result["panelVisibilityChange"] == 0

    def test_auto_refresh_rejects_older_generation_after_manual_refresh(self):
        assert _auto_refresh_harness()["manualNewerAutoOlder"] == 0

    def test_auto_refresh_serializes_overlapping_ticks(self):
        assert _auto_refresh_harness()["overlappingTicks"] == {"fetches": 1, "renders": 1}

    def test_auto_refresh_stop_and_reenable_replaces_owner(self):
        assert _auto_refresh_harness()["stopReenable"] == {
            "activeTimers": 1,
            "fetches": 1,
            "renders": 1,
        }

    def test_context_brief_preference_save_syncs_server_confirmed_timer_state(self):
        assert _autosave_auto_sync_harness() == [False, True]

    def test_switch_static_wiring(self):
        index = open("static/index.html").read()
        assert 'id="settingsContextBriefAuto"' in index
        assert 'settings_label_context_brief_auto' in index
        panels = open("static/panels.js").read()
        assert "payload.context_brief_auto=" in panels
        assert "settings.context_brief_auto===true" in panels
        assert "settingsContextBriefAuto" in panels
        i18n = open("static/i18n.js").read()
        assert "settings_label_context_brief_auto" in i18n
        assert "settings_desc_context_brief_auto" in i18n

    def test_switch_i18n_covers_every_locale(self):
        # Regression 2026-08-15: the first insertion only patched the `en`
        # block, so the switch rendered in English inside a French UI while
        # its neighbour setting was translated. Every locale that translates
        # settings_label_quota_chip must also translate the new switch.
        import re

        src = open("static/i18n.js").read()
        starts = [(m.start(), m.group(1)) for m in re.finditer(r"^  ([a-z][\w-]*): \{$", src, re.M)]
        starts.append((len(src), None))
        missing = []
        for i, (off, loc) in enumerate(starts[:-1]):
            block = src[off:starts[i + 1][0]]
            if "settings_label_quota_chip" not in block:
                continue
            if "settings_label_context_brief_auto" not in block:
                missing.append(loc)
        assert not missing, f"locales missing the auto-brief switch labels: {missing}"
