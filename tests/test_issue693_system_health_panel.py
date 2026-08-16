"""Regression coverage for #693 live VPS host resource health panel."""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import queue
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import urlparse


REPO_ROOT = pathlib.Path(__file__).parent.parent
UI_JS = (REPO_ROOT / "static" / "ui.js").read_text(encoding="utf-8")
PANELS_JS = (REPO_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
INDEX_HTML = (REPO_ROOT / "static" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (REPO_ROOT / "static" / "style.css").read_text(encoding="utf-8")
ROUTES_PY = (REPO_ROOT / "api" / "routes.py").read_text(encoding="utf-8")
AUTH_PY = (REPO_ROOT / "api" / "auth.py").read_text(encoding="utf-8")


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.sent_headers.append((name, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


def test_system_health_payload_normalizes_safe_aggregate_metrics(monkeypatch):
    from api import system_health

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 17.345)
    monkeypatch.setattr(
        system_health,
        "_memory_usage",
        lambda: {"used_bytes": 4_000, "total_bytes": 10_000, "percent": 40.0},
    )
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 55_500, "total_bytes": 100_000, "percent": 55.5},
    )

    payload = system_health.build_system_health_payload()

    assert payload["status"] == "ok"
    assert payload["available"] is True
    assert payload["cpu"] == {"percent": 17.3}
    assert payload["memory"] == {"used_bytes": 4000, "total_bytes": 10000, "percent": 40.0}
    assert payload["disk"] == {"used_bytes": 55500, "total_bytes": 100000, "percent": 55.5}
    assert payload["checked_at"]
    rendered = repr(payload)
    for private_fragment in ("/home/", "/Users/", "mount", "path", "argv", "command", "env", "token"):
        assert private_fragment not in rendered


def test_system_health_payload_partial_and_unavailable_are_graceful(monkeypatch):
    from api import system_health

    def boom():
        raise RuntimeError("private /home/user/path should not leak")

    monkeypatch.setattr(system_health, "_cpu_percent", boom)
    monkeypatch.setattr(system_health, "_memory_usage", boom)
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 1, "total_bytes": 4, "percent": 25.0},
    )

    partial = system_health.build_system_health_payload()
    assert partial["status"] == "partial"
    assert partial["available"] is True
    assert partial["disk"]["percent"] == 25.0
    assert partial["cpu"] is None
    assert partial["memory"] is None
    assert {e["metric"] for e in partial["errors"]} == {"cpu", "memory"}
    assert "/home/user" not in repr(partial)

    monkeypatch.setattr(system_health, "_disk_usage", boom)
    unavailable = system_health.build_system_health_payload()
    assert unavailable["status"] == "unavailable"
    assert unavailable["available"] is False
    assert unavailable["cpu"] is None
    assert unavailable["memory"] is None
    assert unavailable["disk"] is None
    assert "/home/user" not in repr(unavailable)


def test_system_health_falls_back_to_psutil_when_procfs_is_unavailable(monkeypatch):
    from api import system_health

    class _MissingProcPath:
        def open(self, *args, **kwargs):
            raise FileNotFoundError("/private/proc/path")

    class _FakeMemory:
        total = 1000
        available = 250
        percent = 75.0

    fake_psutil = SimpleNamespace(
        cpu_percent=lambda interval=0.0: 42.25,
        virtual_memory=lambda: _FakeMemory(),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr(system_health, "_PROC_STAT", _MissingProcPath())
    monkeypatch.setattr(system_health, "_PROC_MEMINFO", _MissingProcPath())

    assert system_health._cpu_percent() == 42.2
    assert system_health._memory_usage() == {
        "used_bytes": 750,
        "total_bytes": 1000,
        "percent": 75.0,
    }


def test_system_health_missing_optional_psutil_is_safe_unavailable(monkeypatch):
    from api import system_health

    class _MissingProcPath:
        def open(self, *args, **kwargs):
            raise FileNotFoundError("/private/proc/path")

    def missing_psutil(name):
        if name == "psutil":
            raise ModuleNotFoundError("No module named 'psutil'")
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(system_health, "_PROC_STAT", _MissingProcPath())
    monkeypatch.setattr(system_health, "_PROC_MEMINFO", _MissingProcPath())
    monkeypatch.setattr(system_health, "import_module", missing_psutil)

    for collect in (system_health._cpu_percent, system_health._memory_usage):
        try:
            collect()
        except RuntimeError as exc:
            assert str(exc) == "psutil_unavailable"
        else:  # pragma: no cover - defensive regression clarity
            raise AssertionError("missing optional psutil should surface a safe unavailable error")


def test_system_health_procfs_parse_errors_remain_visible(monkeypatch):
    from api import system_health

    fake_psutil = SimpleNamespace(
        cpu_percent=lambda interval=0.0: 42.25,
        virtual_memory=lambda: SimpleNamespace(total=1000, available=250),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    monkeypatch.setattr(
        system_health,
        "_read_proc_stat_cpu",
        lambda: (_ for _ in ()).throw(RuntimeError("proc_stat_unavailable")),
    )
    monkeypatch.setattr(system_health, "_read_meminfo_kib", lambda: {})

    try:
        system_health._cpu_percent()
    except RuntimeError as exc:
        assert str(exc) == "proc_stat_unavailable"
    else:  # pragma: no cover - defensive regression clarity
        raise AssertionError("procfs parse RuntimeError should not fall back to psutil")

    try:
        system_health._memory_usage()
    except RuntimeError as exc:
        assert str(exc) == "meminfo_unavailable"
    else:  # pragma: no cover - defensive regression clarity
        raise AssertionError("meminfo invariant RuntimeError should not fall back to psutil")


def test_system_health_cpu_second_procfs_read_fallback_does_not_sleep_twice(monkeypatch):
    from api import system_health

    calls = []

    def fake_read_proc_stat_cpu():
        calls.append("proc")
        if len(calls) == 1:
            return (10, 100)
        raise FileNotFoundError("/proc/stat disappeared")

    def fake_sleep(seconds):
        calls.append(("sleep", seconds))

    def fake_cpu_percent(interval=0.0):
        calls.append(("psutil", interval))
        return 12.34

    monkeypatch.setattr(system_health, "_read_proc_stat_cpu", fake_read_proc_stat_cpu)
    monkeypatch.setattr(system_health.time, "sleep", fake_sleep)
    monkeypatch.setitem(sys.modules, "psutil", SimpleNamespace(cpu_percent=fake_cpu_percent))

    assert system_health._cpu_percent() == 12.3
    assert calls == ["proc", ("sleep", system_health._CPU_SAMPLE_SECONDS), "proc", ("psutil", 0.0)]


def test_system_health_route_registered_and_auth_gated(monkeypatch):
    assert 'parsed.path == "/api/system/health"' in ROUTES_PY
    assert "build_system_health_payload()" in ROUTES_PY
    assert '"/api/system/health"' not in AUTH_PY, "system metrics must not be public"

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")
    from api import auth as _auth
    from api.auth import check_auth

    # The password hash is cached process-wide (PBKDF2 is ~1s). A prior test may
    # have populated the cache with "no password" (None), so the env var we just
    # set would be ignored on the fast path. Invalidate before AND after so this
    # test sees its own password and doesn't leak the test-password cache to the
    # next test — required for order-independence under sharded/random runs.
    _auth._invalidate_password_hash_cache()

    handler = _FakeHandler()
    try:
        assert check_auth(handler, SimpleNamespace(path="/api/system/health", query="")) is False
        assert handler.status in (302, 401)
    finally:
        monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)
        _auth._invalidate_password_hash_cache()


def test_system_health_route_returns_only_sanitized_payload(monkeypatch):
    from api import routes

    monkeypatch.setattr(
        routes,
        "build_system_health_payload",
        lambda: {
            "status": "ok",
            "available": True,
            "checked_at": "2026-05-05T00:00:00+00:00",
            "cpu": {"percent": 12.0},
            "memory": {"used_bytes": 1, "total_bytes": 2, "percent": 50.0},
            "disk": {"used_bytes": 3, "total_bytes": 4, "percent": 75.0},
            "errors": [],
        },
    )
    handler = _FakeHandler()
    assert routes.handle_get(handler, urlparse("http://example.test/api/system/health")) is True
    payload = handler.json_body()
    assert payload["cpu"]["percent"] == 12.0
    assert set(payload) == {"status", "available", "checked_at", "cpu", "memory", "disk", "errors"}


def test_system_health_panel_markup_and_styles_live_under_insights_not_top_chrome():
    top_shell = INDEX_HTML[: INDEX_HTML.index('<div class="layout">')]
    assert 'id="systemHealthPanel"' not in top_shell
    assert 'aria-label="Host resource health"' not in top_shell
    assert 'function _renderSystemHealthPanel()' in PANELS_JS
    assert 'id="systemHealthPanel"' in PANELS_JS
    assert 'aria-label="Host resource health"' in PANELS_JS
    assert 'System health' in PANELS_JS
    assert 'Current VPS resource usage' in PANELS_JS
    assert PANELS_JS.index('_renderSystemHealthPanel()') < PANELS_JS.index('_renderLlmWikiStatus(wikiStatus)')
    assert 'data-system-health-metric="cpu"' in PANELS_JS
    assert 'data-system-health-metric="memory"' in PANELS_JS
    assert 'data-system-health-metric="disk"' in PANELS_JS
    assert ".system-health-panel.insights-card" in STYLE_CSS
    assert ".system-health-bar-fill" in STYLE_CSS
    assert ".system-health-panel.unavailable" in STYLE_CSS
    assert "@media(max-width:640px)" in STYLE_CSS and ".system-health-panel.insights-card" in STYLE_CSS


def test_system_health_frontend_polls_visible_and_renders_progress_labels():
    assert "const SYSTEM_HEALTH_INTERVAL_MS=5000" in UI_JS
    assert "api('/api/system/health',{timeoutToast:false})" in UI_JS
    assert "document.visibilityState !== 'visible'" in UI_JS
    assert "document.querySelector('main.main.showing-insights')" in UI_JS
    assert "document.addEventListener('visibilitychange',_syncSystemHealthMonitorVisibility)" in UI_JS
    assert "typeof _syncSystemHealthMonitorVisibility === 'function'" in PANELS_JS
    assert "function renderSystemHealth(payload)" in UI_JS
    assert "setSystemHealthUnavailable" in UI_JS
    assert "data-system-health-metric" in PANELS_JS
    assert "CPU" in PANELS_JS and "RAM" in PANELS_JS and "Disk" in PANELS_JS
    assert "aria-valuenow" in UI_JS
    assert "style.width=`${percent}%`" in UI_JS


def test_system_health_backend_uses_no_shell_or_private_process_sources():
    src = (REPO_ROOT / "api" / "system_health.py").read_text(encoding="utf-8")
    assert "import subprocess" not in src
    assert "os.environ" not in src
    assert "ps aux" not in src
    assert "/proc/self/environ" not in src
    for private_field in ("argv", "cmdline", "username", "mountpoint"):
        assert private_field not in src


# #6351 runtime-diagnostics hygiene rule for every test below: a test that
# touches a process-owned shared mapping copies it by value, performs the
# clear/seed and the restore inside the mapping's owning lock, and restores the
# original key order and value identities. Contested locks are always held from
# a worker thread, which is the only schedule production can produce.
_LOCK_HOLD_SECONDS = 3.0
# A nonblocking collector does no I/O, so a second is generous even on a loaded
# CI box, while staying far enough below the hold that a blocking regression
# (which waits the full _LOCK_HOLD_SECONDS) cannot slip past.
_NONBLOCKING_BUDGET_SECONDS = 1.0


@contextlib.contextmanager
def _held_from_worker(lock, hold_seconds=_LOCK_HOLD_SECONDS):
    """Hold ``lock`` from a worker thread for the body of the with-block."""
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with lock:
            entered.set()
            release.wait(hold_seconds)

    worker = threading.Thread(target=hold, daemon=True)
    worker.start()
    try:
        # Inside the try so a worker that never acquires still gets released and
        # joined; otherwise it takes the lock after this test fails and starves
        # the next contention test for the full hold.
        assert entered.wait(5), "worker thread never acquired the contested lock"
        yield
    finally:
        release.set()
        worker.join(hold_seconds + 5)


@contextlib.contextmanager
def _seeded_mapping(lock, mapping, entries):
    """Seed a process-owned mapping under its owning lock and restore it exactly."""
    with lock:
        original = dict(mapping)
        mapping.clear()
        mapping.update(entries)
    try:
        yield original
    finally:
        with lock:
            mapping.clear()
            mapping.update(original)


def _seed_channel_counters(
    channel,
    *,
    subscribers=0,
    offline_buffered=0,
    offline_dropped=0,
    subscriber_dropped=0,
):
    """Seed a real StreamChannel's diagnostic counters under its own lock."""
    with channel._lock:
        channel._subscribers.extend(queue.Queue() for _ in range(subscribers))
        channel._offline_buffer.extend(
            ("token", {"seq": index}, str(index)) for index in range(offline_buffered)
        )
        channel._offline_dropped_total = offline_dropped
        channel._subscriber_dropped_total = subscriber_dropped


def _pin_config_module_cache(monkeypatch):
    """Let pytest restore the config globals a forced reload or eviction rebinds."""
    from api import config

    # _LAST_APPLIED_SESSIONS_CACHE_MAX belongs here because a real
    # _evict_sessions_over_cap() pass rewrites it outside monkeypatch's view;
    # pinning first records the true pre-test value.
    for name in (
        "cfg",
        "_cfg_cache",
        "_cfg_mtime",
        "_cfg_path",
        "_cfg_fingerprint",
        "_LAST_APPLIED_SESSIONS_CACHE_MAX",
    ):
        monkeypatch.setattr(config, name, getattr(config, name), raising=False)


def _fixed_host_metrics(monkeypatch):
    from api import system_health

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 12.0)
    monkeypatch.setattr(
        system_health,
        "_memory_usage",
        lambda: {"used_bytes": 1, "total_bytes": 4, "percent": 25.0},
    )
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 4, "total_bytes": 8, "percent": 50.0},
    )


def _assert_host_metrics_intact(payload):
    assert payload["cpu"] == {"percent": 12.0}
    assert payload["memory"] == {"used_bytes": 1, "total_bytes": 4, "percent": 25.0}
    assert payload["disk"] == {"used_bytes": 4, "total_bytes": 8, "percent": 50.0}


def test_runtime_diagnostics_absence_presence_and_privacy(monkeypatch):
    from api import system_health

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 1.0)
    monkeypatch.setattr(system_health, "_memory_usage", lambda: {"used_bytes": 1, "total_bytes": 2, "percent": 50})
    monkeypatch.setattr(system_health, "_disk_usage", lambda: {"used_bytes": 1, "total_bytes": 2, "percent": 50})
    payload = system_health.build_system_health_payload()
    assert set(payload["webui_runtime"]) == {"sessions", "streams", "session_list_cache", "models_cache"}
    assert "stream_id" not in repr(payload)
    assert "provider" not in repr(payload)
    assert "secret-model" not in repr(payload)
    assert "private-provider" not in repr(payload)


def test_health_route_completes_while_config_cache_lock_is_held(monkeypatch, tmp_path):
    from api import config, routes

    _fixed_host_metrics(monkeypatch)
    _pin_config_module_cache(monkeypatch)

    config_path = tmp_path / "config.yaml"
    config_path.write_text("webui:\n  sessions_cache_max: 41\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(config_path))
    # Cold cache: get_config() takes the reload branch, which waits on _cfg_lock.
    monkeypatch.setattr(config, "_cfg_cache", {})
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    monkeypatch.setattr(config, "_cfg_path", None)
    monkeypatch.setattr(config, "_LAST_APPLIED_SESSIONS_CACHE_MAX", 41, raising=False)

    seeded = {"health-route-a": object(), "health-route-b": object()}
    with _seeded_mapping(config.LOCK, config.SESSIONS, seeded):
        with _held_from_worker(config._cfg_lock):
            handler = _FakeHandler()
            started = time.monotonic()
            assert routes.handle_get(handler, urlparse("http://example.test/api/system/health")) is True
            elapsed = time.monotonic() - started

    assert elapsed < _NONBLOCKING_BUDGET_SECONDS, (
        f"health response waited {elapsed:.3f}s on the config cache lock"
    )
    payload = handler.json_body()
    _assert_host_metrics_intact(payload)
    assert payload["status"] == "ok"
    runtime = payload["webui_runtime"]
    assert runtime["sessions"] == {"available": True, "resident": 2, "cap": 41}
    assert runtime["streams"]["available"] is True
    assert runtime["session_list_cache"]["available"] is True
    assert runtime["session_list_cache"]["cap"] == 64
    assert set(runtime["models_cache"]) == {"available", "groups", "models", "age_seconds"}
    assert "health-route-a" not in repr(payload)


def test_sessions_snapshot_never_resolves_config_or_profile(monkeypatch):
    from api import config, profiles, system_health

    _fixed_host_metrics(monkeypatch)
    fired = []

    def trap(*args, **kwargs):
        fired.append(True)
        raise AssertionError("health collection must not resolve config or profile state")

    monkeypatch.setattr(config, "get_config", trap)
    monkeypatch.setattr(config, "_get_config_path", trap)
    monkeypatch.setattr(profiles, "get_active_hermes_home", trap)
    monkeypatch.setattr(config, "_LAST_APPLIED_SESSIONS_CACHE_MAX", 57, raising=False)

    with _seeded_mapping(config.LOCK, config.SESSIONS, {"trap-probe": object()}):
        payload = system_health.build_system_health_payload()

    assert fired == []
    assert payload["webui_runtime"]["sessions"] == {"available": True, "resident": 1, "cap": 57}
    assert {"metric": "webui_runtime.sessions", "code": "AssertionError"} not in payload["errors"]


def test_health_route_completes_while_one_real_stream_channel_lock_is_held(monkeypatch):
    from api import config, routes

    _fixed_host_metrics(monkeypatch)
    healthy = config.StreamChannel()
    _seed_channel_counters(
        healthy,
        subscribers=2,
        offline_buffered=3,
        offline_dropped=4,
        subscriber_dropped=5,
    )
    busy = config.StreamChannel()

    streams = {"health-open-stream": healthy, "health-busy-stream": busy}
    with _seeded_mapping(config.STREAMS_LOCK, config.STREAMS, streams):
        with _seeded_mapping(config.STREAMS_LOCK, config.AGENT_INSTANCES, {"health-open-stream": object()}):
            with _held_from_worker(busy._lock):
                handler = _FakeHandler()
                started = time.monotonic()
                assert routes.handle_get(handler, urlparse("http://example.test/api/system/health")) is True
                elapsed = time.monotonic() - started

    assert elapsed < _NONBLOCKING_BUDGET_SECONDS, (
        f"health response waited {elapsed:.3f}s on one busy stream channel"
    )
    payload = handler.json_body()
    _assert_host_metrics_intact(payload)
    assert payload["status"] == "ok"
    runtime = payload["webui_runtime"]
    assert runtime["streams"] == {
        "available": True,
        "active": 2,
        "agent_instances": 1,
        "subscribers": 2,
        "offline_buffered_events": 3,
        "offline_dropped_events": 4,
        "subscriber_dropped_events": 5,
        "unavailable_channels": 1,
    }
    assert runtime["sessions"]["available"] is True
    assert runtime["session_list_cache"]["available"] is True
    assert set(runtime["models_cache"]) == {"available", "groups", "models", "age_seconds"}
    assert "health-open-stream" not in repr(payload)
    assert "health-busy-stream" not in repr(payload)


def test_stream_channel_try_snapshot_matches_blocking_snapshot():
    from api import config

    channel = config.StreamChannel()
    _seed_channel_counters(
        channel,
        subscribers=1,
        offline_buffered=2,
        offline_dropped=3,
        subscriber_dropped=4,
    )
    blocking = channel.diagnostic_snapshot()
    nonblocking = channel.try_diagnostic_snapshot()
    assert nonblocking == blocking
    assert set(nonblocking) == {
        "subscriber_count",
        "offline_buffered_events",
        "offline_dropped_events",
        "subscriber_dropped_events",
    }
    assert blocking == {
        "subscriber_count": 1,
        "offline_buffered_events": 2,
        "offline_dropped_events": 3,
        "subscriber_dropped_events": 4,
    }

    # diagnostic_snapshot() is deliberately not called here: it keeps its
    # blocking contract for /health?deep=1 and would wait out the whole hold.
    with _held_from_worker(channel._lock):
        started = time.monotonic()
        assert channel.try_diagnostic_snapshot() is None
        assert time.monotonic() - started < _NONBLOCKING_BUDGET_SECONDS


def test_sessions_owner_snapshot_preserves_existing_cache_identities(monkeypatch):
    from api import config

    monkeypatch.setattr(config, "_LAST_APPLIED_SESSIONS_CACHE_MAX", 37, raising=False)
    probe_key = "pre-existing-identity-probe"
    probe = object()
    with config.LOCK:
        config.SESSIONS[probe_key] = probe
    try:
        seeded = {"first": object(), "second": object()}
        with _seeded_mapping(config.LOCK, config.SESSIONS, seeded) as original:
            assert probe_key not in config.SESSIONS
            assert config.get_runtime_diagnostics_snapshot()["sessions"] == {
                "available": True, "resident": 2, "cap": 37
            }
            assert list(config.SESSIONS) == ["first", "second"]

        assert list(config.SESSIONS) == list(original)
        for key, value in original.items():
            assert config.SESSIONS[key] is value
        assert config.SESSIONS[probe_key] is probe
    finally:
        with config.LOCK:
            config.SESSIONS.pop(probe_key, None)


def test_diagnostics_cap_tracks_the_eviction_owner(monkeypatch, tmp_path):
    from api import config, models

    _pin_config_module_cache(monkeypatch)
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(config_path))
    fallback = (
        config.SESSIONS_MAX
        if isinstance(config.SESSIONS_MAX, int) and config.SESSIONS_MAX >= 1
        else config.DEFAULT_SESSIONS_CACHE_MAX
    )

    for body, expected in (
        ("webui:\n  sessions_cache_max: 23\n", 23),
        ("webui:\n  sessions_cache_max: 0\n", fallback),
        ("webui: {}\n", fallback),
    ):
        config_path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(config, "_cfg_cache", {})
        monkeypatch.setattr(config, "_cfg_path", None)
        assert config.get_sessions_cache_max() == expected
        # Poison the memo so only a real eviction pass can restore it.
        monkeypatch.setattr(config, "_LAST_APPLIED_SESSIONS_CACHE_MAX", -1)
        with _seeded_mapping(config.LOCK, config.SESSIONS, {}):
            with config.LOCK:
                models._evict_sessions_over_cap()
            assert config.get_runtime_diagnostics_snapshot()["sessions"]["cap"] == expected


def test_diagnostics_cap_reports_the_owner_fallback_when_the_getter_raises(monkeypatch):
    from api import config, models, system_health

    _fixed_host_metrics(monkeypatch)
    _pin_config_module_cache(monkeypatch)

    def raiser(*args, **kwargs):
        raise RuntimeError("cap resolution failed")

    # A distinct prior memo, so no assertion below can pass on the incumbent value.
    monkeypatch.setattr(config, "_LAST_APPLIED_SESSIONS_CACHE_MAX", 4321, raising=False)
    # api/models.py resolves the getter through _cfg at call time, so failing the
    # real attribute is what the production lookup hits, not a shadowed copy.
    monkeypatch.setattr(config, "get_sessions_cache_max", raiser)
    # Pinning models' own binding is what proves the published cap came from the
    # enforcer's fallback rather than from anything the resolver returned.
    monkeypatch.setattr(models, "SESSIONS_MAX", 29)

    with _seeded_mapping(config.LOCK, config.SESSIONS, {}):
        with config.LOCK:
            models._evict_sessions_over_cap()
        snapshot = config.get_runtime_diagnostics_snapshot()
        payload = system_health.build_system_health_payload()

    assert snapshot["sessions"]["cap"] != 4321
    assert snapshot["sessions"]["cap"] == 29
    assert payload["webui_runtime"]["sessions"]["cap"] == 29


def test_diagnostics_cap_reports_the_normalized_explicit_cap(monkeypatch):
    from api import config, models, system_health

    _fixed_host_metrics(monkeypatch)
    _pin_config_module_cache(monkeypatch)
    # An explicit cap never reaches the resolver, so only the enforcer can publish.
    monkeypatch.setattr(models, "SESSIONS_MAX", 17)

    for explicit, expected in ((0, 17), ("nope", 17), (5, 5)):
        monkeypatch.setattr(config, "_LAST_APPLIED_SESSIONS_CACHE_MAX", 4321, raising=False)
        with _seeded_mapping(config.LOCK, config.SESSIONS, {}):
            with config.LOCK:
                models._evict_sessions_over_cap(cap=explicit)
            snapshot = config.get_runtime_diagnostics_snapshot()
            payload = system_health.build_system_health_payload()

        assert snapshot["sessions"]["cap"] != 4321, explicit
        assert snapshot["sessions"]["cap"] == expected, explicit
        assert payload["webui_runtime"]["sessions"]["cap"] == expected, explicit


def test_the_cap_getter_does_not_publish_diagnostics_state(monkeypatch, tmp_path):
    from api import config

    _pin_config_module_cache(monkeypatch)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("webui:\n  sessions_cache_max: 88\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(config_path))
    monkeypatch.setattr(config, "_cfg_cache", {})
    monkeypatch.setattr(config, "_cfg_mtime", 0.0)
    monkeypatch.setattr(config, "_cfg_path", None)
    monkeypatch.setattr(config, "_LAST_APPLIED_SESSIONS_CACHE_MAX", 4321, raising=False)

    assert config.get_sessions_cache_max({"webui": {"sessions_cache_max": 42}}) == 42
    assert config._LAST_APPLIED_SESSIONS_CACHE_MAX == 4321
    assert config.get_sessions_cache_max() == 88
    assert config._LAST_APPLIED_SESSIONS_CACHE_MAX == 4321

    # Process start: a fresh import must already carry the getter's dict-mode
    # answer for the config it loaded, not the SESSIONS_MAX/default tier. Read in
    # a child process because this one's memo has been rewritten by earlier tests.
    seeded = subprocess.run(
        [sys.executable, "-c", "import api.config as c; print(c._LAST_APPLIED_SESSIONS_CACHE_MAX)"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "HERMES_CONFIG_PATH": str(config_path)},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert seeded.returncode == 0, seeded.stderr
    printed = seeded.stdout.strip().splitlines()
    assert printed, f"child printed nothing; stderr={seeded.stderr!r}"
    assert int(printed[-1]) == 88


def test_infinite_cap_falls_back_instead_of_raising(tmp_path):
    """A YAML float infinity must degrade to the bound, not abort the import.

    ``yaml.safe_load`` resolves ``.inf`` to a real float and ``int(float('inf'))``
    raises OverflowError, which is neither TypeError nor ValueError. The cap is
    resolved at module scope, so an uncaught raise here takes down startup.
    """
    from api import config

    fallback = (
        config.SESSIONS_MAX
        if isinstance(config.SESSIONS_MAX, int) and config.SESSIONS_MAX >= 1
        else config.DEFAULT_SESSIONS_CACHE_MAX
    )
    for raw in (float("inf"), float("-inf"), float("nan")):
        assert config.get_sessions_cache_max({"webui": {"sessions_cache_max": raw}}) == fallback

    config_path = tmp_path / "config.yaml"
    config_path.write_text("webui:\n  sessions_cache_max: .inf\n", encoding="utf-8")
    booted = subprocess.run(
        [sys.executable, "-c", "import api.config as c; print(c._LAST_APPLIED_SESSIONS_CACHE_MAX)"],
        cwd=str(REPO_ROOT),
        env={**os.environ, "HERMES_CONFIG_PATH": str(config_path)},
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert booted.returncode == 0, booted.stderr
    printed = booted.stdout.strip().splitlines()
    assert printed, f"child printed nothing; stderr={booted.stderr!r}"
    assert int(printed[-1]) == fallback


def test_sessions_lock_busy_does_not_block_sibling_owner():
    from api import config

    with _held_from_worker(config.LOCK):
        started = time.monotonic()
        snapshot = config.get_runtime_diagnostics_snapshot()
        elapsed = time.monotonic() - started

    assert elapsed < _NONBLOCKING_BUDGET_SECONDS
    assert snapshot["sessions"] == {"available": False, "resident": 0, "cap": 0}
    assert "models_cache" in snapshot


def test_health_status_stays_ok_when_sessions_owner_lock_is_busy(monkeypatch):
    from api import config, system_health

    _fixed_host_metrics(monkeypatch)
    with _held_from_worker(config.LOCK):
        started = time.monotonic()
        payload = system_health.build_system_health_payload()
        elapsed = time.monotonic() - started

    assert elapsed < _NONBLOCKING_BUDGET_SECONDS
    assert payload["status"] == "ok"
    assert payload["webui_runtime"]["sessions"]["available"] is False
    assert payload["webui_runtime"]["models_cache"]["available"] is True


def test_models_cache_snapshot_counts_published_groups_without_names():
    from api import config

    groups = [
        {
            "name": "private-provider",
            "models": [{"id": "secret-model"}],
            "extra_models": [{"id": "overflow-secret"}, {"id": "other"}],
        },
        {"models": [], "extra_models": [{"id": "overflow-two"}]},
    ]
    baseline = time.monotonic()
    with config._available_models_cache_lock:
        original_cache = config._available_models_cache
        original_ts = config._available_models_cache_ts
        config._available_models_cache = {"groups": groups}
        config._available_models_cache_ts = baseline - 30.0
    try:
        result = config.get_runtime_diagnostics_snapshot()["models_cache"]
    finally:
        with config._available_models_cache_lock:
            config._available_models_cache = original_cache
            config._available_models_cache_ts = original_ts

    assert result["available"] is True
    assert result["groups"] == 2
    assert result["models"] == 4
    # Real clock, bounded range: patching time.monotonic would freeze the stdlib
    # clock for every other thread in the process.
    assert 30.0 <= result["age_seconds"] < 30.0 + _NONBLOCKING_BUDGET_SECONDS
    assert "private-provider" not in repr(result)
    assert "secret-model" not in repr(result)


def test_stream_owner_snapshot_aggregates_channels():
    from api import config, streaming

    first = config.StreamChannel()
    _seed_channel_counters(
        first,
        subscribers=2,
        offline_buffered=3,
        offline_dropped=4,
        subscriber_dropped=5,
    )
    second = config.StreamChannel()
    _seed_channel_counters(second, subscribers=1, subscriber_dropped=7)

    streams = {"secret-id": first, "other-secret-id": second}
    with _seeded_mapping(config.STREAMS_LOCK, config.STREAMS, streams):
        with _seeded_mapping(config.STREAMS_LOCK, config.AGENT_INSTANCES, {"secret-id": object()}):
            result = streaming.get_stream_runtime_snapshot()

    assert result == {
        "available": True, "active": 2, "agent_instances": 1,
        "subscribers": 3, "offline_buffered_events": 3,
        "offline_dropped_events": 4, "subscriber_dropped_events": 12,
        "unavailable_channels": 0,
    }
    assert "secret-id" not in repr(result)


def test_stream_registry_lock_busy_isolated():
    from api import config, streaming

    with _held_from_worker(config.STREAMS_LOCK):
        started = time.monotonic()
        result = streaming.get_stream_runtime_snapshot()
        elapsed = time.monotonic() - started

    assert elapsed < _NONBLOCKING_BUDGET_SECONDS
    assert result["available"] is False
    assert result["active"] == 0


def test_session_list_cache_snapshot_reads_scalars_without_lru_mutation():
    from api import route_session_list_cache as cache

    probe_key = ("pre-existing-identity-probe",)
    probe = (0.0, (), {})
    with cache._SESSIONS_CACHE_LOCK:
        cache._SESSIONS_CACHE[probe_key] = probe
    try:
        with _seeded_mapping(
            cache._SESSIONS_CACHE_LOCK, cache._SESSIONS_CACHE, {("one",): (0.0, (), {})}
        ) as original:
            result = cache.get_session_list_cache_snapshot()
            assert result == {"available": True, "entries": 1, "inflight_rebuilds": 0, "cap": 64}
            assert list(cache._SESSIONS_CACHE) == [("one",)]

        assert list(cache._SESSIONS_CACHE) == list(original)
        assert cache._SESSIONS_CACHE[probe_key] is probe
    finally:
        with cache._SESSIONS_CACHE_LOCK:
            cache._SESSIONS_CACHE.pop(probe_key, None)


def test_runtime_unexpected_exception_fail_open(monkeypatch):
    from api import config, system_health

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 1.0)
    monkeypatch.setattr(config, "get_runtime_diagnostics_snapshot", lambda: (_ for _ in ()).throw(RecursionError("secret")))
    payload = system_health.build_system_health_payload()
    assert payload["status"] == "partial"
    assert payload["webui_runtime"]["sessions"]["available"] is False
    assert {"metric": "webui_runtime.sessions", "code": "RecursionError"} in payload["errors"]
    assert "secret" not in repr(payload)


def test_runtime_owner_failure_preserves_sibling_metrics(monkeypatch):
    from api import config, route_session_list_cache, streaming, system_health

    monkeypatch.setattr(config, "get_runtime_diagnostics_snapshot", lambda: (_ for _ in ()).throw(RecursionError("secret config")))
    monkeypatch.setattr(streaming, "get_stream_runtime_snapshot", lambda: {"available": True, "active": 4, "agent_instances": 3, "subscribers": 0, "offline_buffered_events": 0, "offline_dropped_events": 0, "subscriber_dropped_events": 0, "unavailable_channels": 0})
    monkeypatch.setattr(route_session_list_cache, "get_session_list_cache_snapshot", lambda: {"available": True, "entries": 2, "inflight_rebuilds": 0, "cap": 64})
    payload = system_health.build_system_health_payload()
    runtime = payload["webui_runtime"]
    assert runtime["sessions"]["available"] is False
    assert runtime["models_cache"]["available"] is False
    assert runtime["streams"]["active"] == 4
    assert runtime["session_list_cache"]["entries"] == 2
    assert "secret config" not in repr(payload)


def test_config_owner_collected_once_per_health_payload(monkeypatch):
    from api import config, system_health

    _fixed_host_metrics(monkeypatch)
    real = config.get_runtime_diagnostics_snapshot
    calls = []

    def counting_wrapper():
        calls.append(True)
        snapshot = real()
        # Successive calls disagree, so a second collection would show up as a
        # sessions/models_cache skew in the response.
        snapshot["sessions"]["resident"] = 10 * len(calls)
        snapshot["models_cache"]["groups"] = 10 * len(calls)
        return snapshot

    monkeypatch.setattr(config, "get_runtime_diagnostics_snapshot", counting_wrapper)
    payload = system_health.build_system_health_payload()

    assert len(calls) == 1
    runtime = payload["webui_runtime"]
    assert runtime["sessions"]["resident"] == 10
    assert runtime["models_cache"]["groups"] == 10


def test_webui_runtime_compositor_failure_preserves_host_metrics(monkeypatch):
    from api import system_health

    _fixed_host_metrics(monkeypatch)

    def boom(errors):
        raise RecursionError("secret compositor path")

    monkeypatch.setattr(system_health, "_webui_runtime_payload", boom)
    payload = system_health.build_system_health_payload()

    _assert_host_metrics_intact(payload)
    assert payload["status"] == "partial"
    assert payload["webui_runtime"] == system_health._zero_webui_runtime_payload()
    assert {"metric": "webui_runtime", "code": "RecursionError"} in payload["errors"]
    assert "secret compositor path" not in repr(payload)


def test_cancel_cleanup_updates_runtime_diagnostics():
    from api import config, streaming

    stream_id = "production-cleanup-stream"
    session_id = "production-cleanup-session"
    agent = Mock()
    agent.session_id = session_id
    session = SimpleNamespace(
        session_id=session_id,
        active_stream_id=stream_id,
        pending_user_message=None,
        pending_user_source=None,
        pending_attachments=[],
        pending_started_at=None,
        messages=[],
        save=Mock(),
    )
    with contextlib.ExitStack() as stack:
        stack.enter_context(
            _seeded_mapping(config.STREAMS_LOCK, config.STREAMS, {stream_id: config.StreamChannel()})
        )
        stack.enter_context(
            _seeded_mapping(config.STREAMS_LOCK, config.AGENT_INSTANCES, {stream_id: agent})
        )
        stack.enter_context(
            _seeded_mapping(config.STREAMS_LOCK, config.CANCEL_FLAGS, {stream_id: threading.Event()})
        )
        stack.enter_context(_seeded_mapping(config.ACTIVE_RUNS_LOCK, config.ACTIVE_RUNS, {}))
        before = streaming.get_stream_runtime_snapshot()
        with patch("api.streaming.get_session", return_value=session):
            result = streaming.cancel_stream(stream_id)
            assert result["cancelled"] is True
            assert result["persistence_failed"] is False
            assert result["stream_id"] == stream_id
        after = streaming.get_stream_runtime_snapshot()

    assert before["active"] == 1
    assert before["agent_instances"] == 1
    assert after["active"] == 0
    assert after["agent_instances"] == 0
    agent.interrupt.assert_called_once_with("Cancelled by user")


def test_models_cache_lock_busy():
    from api import config

    with _held_from_worker(config._available_models_cache_lock):
        started = time.monotonic()
        snapshot = config.get_runtime_diagnostics_snapshot()
        elapsed = time.monotonic() - started

    assert elapsed < _NONBLOCKING_BUDGET_SECONDS
    assert snapshot["models_cache"]["available"] is False


def test_session_list_cache_lock_busy():
    from api import route_session_list_cache as cache

    with _held_from_worker(cache._SESSIONS_CACHE_LOCK):
        started = time.monotonic()
        result = cache.get_session_list_cache_snapshot()
        elapsed = time.monotonic() - started

    assert elapsed < _NONBLOCKING_BUDGET_SECONDS
    assert result["available"] is False


def test_stream_snapshot_failure():
    from api import config, streaming

    class _FailingChannel(config.StreamChannel):
        def try_diagnostic_snapshot(self):
            raise RecursionError("secret path")

    healthy = config.StreamChannel()
    _seed_channel_counters(
        healthy,
        subscribers=2,
        offline_buffered=3,
        offline_dropped=4,
        subscriber_dropped=5,
    )
    streams = {"healthy-secret-id": healthy, "failing-secret-id": _FailingChannel()}
    with _seeded_mapping(config.STREAMS_LOCK, config.STREAMS, streams):
        with _seeded_mapping(config.STREAMS_LOCK, config.AGENT_INSTANCES, {"healthy-secret-id": object()}):
            result = streaming.get_stream_runtime_snapshot()

    assert result == {
        "available": True, "active": 2, "agent_instances": 1,
        "subscribers": 2, "offline_buffered_events": 3,
        "offline_dropped_events": 4, "subscriber_dropped_events": 5,
        "unavailable_channels": 1,
    }
    rendered = repr(result)
    assert "secret path" not in rendered
    assert "RecursionError" not in rendered
    assert "secret-id" not in rendered


def test_runtime_route_auth_and_privacy(monkeypatch):
    from api import auth as _auth
    from api.auth import check_auth
    from api import system_health

    monkeypatch.setenv("HERMES_WEBUI_PASSWORD", "test-password")
    _auth._invalidate_password_hash_cache()
    handler = _FakeHandler()
    try:
        assert check_auth(handler, SimpleNamespace(path="/api/system/health", query="")) is False
        assert handler.status in (302, 401)
    finally:
        monkeypatch.delenv("HERMES_WEBUI_PASSWORD", raising=False)
        _auth._invalidate_password_hash_cache()

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 1.0)
    payload = system_health.build_system_health_payload()
    assert payload["webui_runtime"]["streams"].keys() >= {
        "available", "active", "agent_instances", "subscribers"
    }
    assert all(key not in repr(payload) for key in ("stream-id", "session-id", "secret-model"))


def test_runtime_owner_adapter_routing(monkeypatch):
    from api import config, route_session_list_cache, streaming, system_health

    monkeypatch.setattr(config, "get_runtime_diagnostics_snapshot", lambda: {
        "sessions": {"available": True, "resident": 9, "cap": 11},
        "models_cache": {"available": True, "groups": 3, "models": 4, "age_seconds": 5},
    })
    monkeypatch.setattr(streaming, "get_stream_runtime_snapshot", lambda: {
        "available": True, "active": 7, "agent_instances": 6, "subscribers": 5,
        "offline_buffered_events": 4, "offline_dropped_events": 3,
        "subscriber_dropped_events": 2, "unavailable_channels": 1,
    })
    monkeypatch.setattr(route_session_list_cache, "get_session_list_cache_snapshot", lambda: {
        "available": True, "entries": 8, "inflight_rebuilds": 1, "cap": 64,
    })
    runtime = system_health._webui_runtime_payload([])
    assert runtime["sessions"]["resident"] == 9
    assert runtime["streams"]["active"] == 7
    assert runtime["session_list_cache"]["entries"] == 8


def test_runtime_diagnostics_ignore_profile_storage(monkeypatch, tmp_path):
    from api import config, profiles, system_health

    _fixed_host_metrics(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    leftover_profile = hermes_home / "profiles" / "work"
    (leftover_profile / "sessions").mkdir(parents=True)
    (leftover_profile / "config.yaml").write_text(
        "webui:\n  sessions_cache_max: 7\n", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    fired = []

    def trap(*args, **kwargs):
        fired.append(True)
        raise AssertionError("health collection must not resolve profile storage")

    monkeypatch.setattr(profiles, "get_active_hermes_home", trap)
    monkeypatch.setattr(config, "_get_config_path", trap)

    payload = system_health.build_system_health_payload()

    assert fired == []
    assert payload["status"] in {"ok", "partial"}
    rendered = repr(payload)
    assert "profiles" not in rendered
    assert str(hermes_home) not in rendered
    assert "work" not in rendered
