# Auto end-of-turn brief regeneration (validated 2026-08-14): worker guards,
# bounded model selection, and route payload.
import threading
import time
from types import SimpleNamespace

import pytest

from api import context_brief as cb


@pytest.fixture(autouse=True)
def _clean_auto_state(monkeypatch, tmp_path):
    monkeypatch.setattr(cb, "_AUTO_LAST_ENQUEUE_AT", {})
    monkeypatch.setattr(cb, "_AUTO_LAST_SEEN_FINISH", 0.0)
    with cb._JOBS_LOCK:
        cb._JOBS.clear()
    yield


def _session(sid="20260814_000000_abc123", n_messages=5):
    """Attribute-access session object, as returned by _resolve_session."""
    return SimpleNamespace(id=sid, messages=[{"role": "user", "content": f"m{i}"} for i in range(n_messages)])


def _listed(sid, n_messages=5):
    """Metadata dict as returned by all_sessions() (no messages loaded)."""
    return {"session_id": sid, "message_count": n_messages, "updated_at": time.time()}


class TestAutoConfig:
    def test_defaults(self, monkeypatch):
        monkeypatch.setattr("api.config.load_settings", lambda: {})
        cfg = cb.get_auto_config()
        assert cfg["enabled"] is True
        assert cfg["model"] == "gpt-5.6-luna"
        assert cfg["effort"] == "low"
        assert cfg["min_interval_seconds"] == 60.0
        assert cfg["choices"] == ["auxiliary", "gpt-5.6-luna"]

    def test_invalid_model_falls_back(self, monkeypatch):
        monkeypatch.setattr("api.config.load_settings", lambda: {"context_brief_model": "gpt-5.6-sol"})
        assert cb.get_auto_config()["model"] == "gpt-5.6-luna"

    def test_interval_clamped(self, monkeypatch):
        monkeypatch.setattr(
            "api.config.load_settings",
            lambda: {"context_brief_min_interval_seconds": 5, "context_brief_auto": False},
        )
        cfg = cb.get_auto_config()
        assert cfg["min_interval_seconds"] == 30.0
        assert cfg["enabled"] is False


class TestModelThreading:
    def _fake_call_llm(self, monkeypatch, captured):
        import agent.auxiliary_client as aux

        def fake(**kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "x" * 300}}]}

        monkeypatch.setattr(aux, "call_llm", fake)
        monkeypatch.setattr(cb, "_distill_transcript", lambda s: "distilled")
        monkeypatch.setattr(cb, "_extract_llm_content", lambda r: "x" * 300)

    def test_generate_passes_model_and_effort(self, monkeypatch):
        captured = {}
        self._fake_call_llm(monkeypatch, captured)
        text, source = cb._generate_llm_brief(
            _session(), "sid", {"meta": {"title": "t", "message_count": 5}},
            model="gpt-5.6-luna", effort="low",
        )
        assert source == "auxiliary-llm"
        assert captured["model"] == "gpt-5.6-luna"
        assert captured["reasoning_config"] == {"effort": "low"}
        assert captured["task"] == "compression"

    def test_auxiliary_choice_omits_model_override(self, monkeypatch):
        captured = {}
        self._fake_call_llm(monkeypatch, captured)
        cb._generate_llm_brief(
            _session(), "sid", {"meta": {"title": "t", "message_count": 5}}, model="auxiliary"
        )
        assert captured  # the fake really ran (no real provider call in tests)
        assert "model" not in captured


class TestWorkerGuards:
    def _arm(self, monkeypatch, sessions, stored=None, active_sids=(), enabled=True):
        monkeypatch.setattr("api.config.load_settings", lambda: {"context_brief_auto": enabled})
        monkeypatch.setattr(cb, "load_llm_brief", lambda s, sid: (stored or {}).get(sid))
        monkeypatch.setattr("api.models.all_sessions", lambda **kw: sessions)

        import api.config as cfg_mod

        cfg_mod.LAST_RUN_FINISHED_AT = time.time() + 100  # a run "just ended"
        monkeypatch.setattr(
            cfg_mod, "ACTIVE_RUNS", {f"st-{sid}": {"session_id": sid} for sid in active_sids}
        )
        started = []
        monkeypatch.setattr(cb, "start_brief_job", lambda sid: started.append(sid) or {"job_id": "j"})
        return started

    def test_enqueues_only_stale_idle_sessions(self, monkeypatch):
        fresh = _listed("s_fresh", 5)
        stale = _listed("s_stale", 7)
        busy = _listed("s_busy", 3)
        stored = {
            "s_fresh": {"message_count_at_generation": 5},   # matches → skip
            "s_stale": {"message_count_at_generation": 4},   # moved → regenerate
            "s_busy": None,                                   # never generated → would enqueue
        }
        started = self._arm(
            monkeypatch, [fresh, stale, busy], stored=stored, active_sids=("s_busy",)
        )
        cb._auto_tick()
        assert started == ["s_stale"]  # busy excluded by active-run guard

    def test_no_scan_without_turn_end(self, monkeypatch):
        started = self._arm(monkeypatch, [_session()])
        import api.config as cfg_mod

        cfg_mod.LAST_RUN_FINISHED_AT = 0.0
        cb._auto_tick()
        assert started == []

    def test_min_interval_debounce(self, monkeypatch):
        s = _listed("s1", 5)
        started = self._arm(monkeypatch, [s], stored={"s1": {"message_count_at_generation": 1}})
        cb._auto_tick()
        cb._auto_tick()  # second tick: same finish gate → no re-scan
        assert started == ["s1"]
        # Even with a newer finish ts, the interval blocks an immediate second run
        import api.config as cfg_mod

        cfg_mod.LAST_RUN_FINISHED_AT = time.time() + 200
        cb._auto_tick()
        assert started == ["s1"]

    def test_burst_cap(self, monkeypatch):
        sessions = [_listed(f"s{i}", 5) for i in range(5)]
        started = self._arm(monkeypatch, sessions, stored={})
        cb._auto_tick()
        assert len(started) == cb._AUTO_MAX_PER_TICK

    def test_disabled(self, monkeypatch):
        started = self._arm(monkeypatch, [_listed("s0")], stored={}, enabled=False)
        cb._auto_tick()
        assert started == []

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
    def test_model_select_and_auto_refresh_present(self):
        src = open("static/panels.js").read()
        assert "function _contextBriefModelSelect(brief)" in src
        assert "onchange=\"_contextBriefModelChange(this)\"" in src
        assert "_contextBriefAutoTimer = setInterval" in src
        assert 'body: JSON.stringify({context_brief_model: value})' in src

    def test_i18n_keys_all_locales(self):
        src = open("static/i18n.js").read()
        for key in ("context_brief_model_label:", "context_brief_model_aux:", "context_brief_model_saved:"):
            assert src.count(key) == 3, key

    def test_css(self):
        assert ".ctx-brief-model-select" in open("static/style.css").read()

    def test_refresh_reads_match_payload_key(self):
        # The auto-refresh must read brief.llm_brief (the only key the server
        # emits) and call renderContextBrief(brief, panel) in that order.
        src = open("static/panels.js").read()
        assert "data.brief.llm_brief" in src
        assert "data.brief.llm)" not in src
        assert "renderContextBrief(data.brief, p)" in src
        assert "renderContextBrief(p, data.brief)" not in src

    def test_settings_keys_registered(self):
        src = open("api/config.py").read()
        for key in ('"context_brief_auto"', '"context_brief_model"', '"context_brief_min_interval_seconds"'):
            assert key in src, key
        assert '"context_brief_model": {"auxiliary", "gpt-5.6-luna"}' in src
