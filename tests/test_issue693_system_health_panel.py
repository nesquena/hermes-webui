"""Regression coverage for #693 live VPS host resource health panel."""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import threading
from types import SimpleNamespace
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
    monkeypatch.setattr(
        system_health,
        "_webui_runtime_payload",
        lambda: {
            "sessions": {"resident_count": 2, "effective_cap": 100},
            "session_list_cache": {
                "entries": 1,
                "entry_cap": 64,
                "inflight_rebuilds": 0,
            },
            "streams": {
                "active_streams": 0,
                "total_subscribers": 0,
                "total_offline_buffered_events": 0,
                "total_offline_dropped_events": 0,
                "per_stream_offline_buffer_cap": 8192,
            },
            "models_cache": {
                "loaded": False,
                "provider_groups": 0,
                "total_models": 0,
                "age_seconds": None,
            },
        },
    )

    payload = system_health.build_system_health_payload()

    assert payload["status"] == "ok"
    assert payload["available"] is True
    assert payload["cpu"] == {"percent": 17.3}
    assert payload["memory"] == {
        "used_bytes": 4000,
        "total_bytes": 10000,
        "percent": 40.0,
    }
    assert payload["disk"] == {
        "used_bytes": 55500,
        "total_bytes": 100000,
        "percent": 55.5,
    }
    assert payload["webui_runtime"]["sessions"] == {
        "resident_count": 2,
        "effective_cap": 100,
    }
    assert payload["checked_at"]
    rendered = repr(payload)
    for private_fragment in (
        "/home/",
        "/Users/",
        "mount",
        "path",
        "argv",
        "command",
        "env",
        "token",
    ):
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
    monkeypatch.setattr(
        system_health,
        "_webui_runtime_payload",
        system_health._zero_webui_runtime_payload,
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


def test_system_health_payload_includes_webui_runtime_counts(monkeypatch):
    from api import system_health

    class _FakeChannel:
        def __init__(self, subscribers, buffered, dropped):
            self._snapshot = {
                "subscriber_count": subscribers,
                "offline_buffered_events": buffered,
                "offline_dropped_events": dropped,
            }

        def diagnostic_snapshot(self):
            return dict(self._snapshot)

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 10.0)
    monkeypatch.setattr(
        system_health,
        "_memory_usage",
        lambda: {"used_bytes": 100, "total_bytes": 200, "percent": 50.0},
    )
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 30, "total_bytes": 60, "percent": 50.0},
    )
    monkeypatch.setattr(system_health.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(
        system_health,
        "_webui_runtime_sources",
        lambda: {
            "sessions": {"a": object(), "b": object(), "c": object()},
            "sessions_lock": threading.Lock(),
            "sessions_effective_cap": 123,
            "streams": {
                "one": _FakeChannel(2, 5, 1),
                "two": _FakeChannel(1, 7, 3),
            },
            "streams_lock": threading.Lock(),
            "stream_buffer_cap": 8192,
            "session_list_cache": {("default",): object(), ("work",): object()},
            "session_list_cache_inflight": {("default",): object()},
            "session_list_cache_lock": threading.Lock(),
            "session_list_cache_cap": 64,
            "models_cache_snapshot": lambda: (
                {
                    "active_provider": "openai",
                    "default_model": "gpt-5.5",
                    "configured_model_badges": {},
                    "groups": [
                        {
                            "provider_id": "openai",
                            "models": [{"id": "gpt-5.5"}, {"id": "gpt-5.5-mini"}],
                            "extra_models": [{"id": "gpt-5.4"}],
                        },
                        {
                            "provider_id": "anthropic",
                            "models": [{"id": "claude"}],
                        },
                    ],
                },
                90.0,
            ),
            "is_valid_models_cache": lambda snapshot: (
                isinstance(snapshot, dict) and isinstance(snapshot.get("groups"), list)
            ),
        },
        raising=False,
    )

    payload = system_health.build_system_health_payload()

    assert payload["webui_runtime"] == {
        "sessions": {"resident_count": 3, "effective_cap": 123},
        "session_list_cache": {"entries": 2, "entry_cap": 64, "inflight_rebuilds": 1},
        "streams": {
            "active_streams": 2,
            "total_subscribers": 3,
            "total_offline_buffered_events": 12,
            "total_offline_dropped_events": 4,
            "per_stream_offline_buffer_cap": 8192,
        },
        "models_cache": {
            "loaded": True,
            "provider_groups": 2,
            "total_models": 4,
            "age_seconds": 10.0,
        },
    }


def test_system_health_payload_reports_cold_webui_runtime_state(monkeypatch):
    from api import system_health

    monkeypatch.setattr(
        system_health,
        "_webui_runtime_sources",
        lambda: {
            "sessions": {},
            "sessions_lock": threading.Lock(),
            "sessions_effective_cap": 100,
            "streams": {},
            "streams_lock": threading.Lock(),
            "stream_buffer_cap": 8192,
            "session_list_cache": {},
            "session_list_cache_inflight": {},
            "session_list_cache_lock": threading.Lock(),
            "session_list_cache_cap": 64,
            "models_cache_snapshot": lambda: (None, 0.0),
            "is_valid_models_cache": lambda snapshot: False,
        },
        raising=False,
    )

    payload = system_health._webui_runtime_payload()

    assert payload == {
        "sessions": {"resident_count": 0, "effective_cap": 100},
        "session_list_cache": {"entries": 0, "entry_cap": 64, "inflight_rebuilds": 0},
        "streams": {
            "active_streams": 0,
            "total_subscribers": 0,
            "total_offline_buffered_events": 0,
            "total_offline_dropped_events": 0,
            "per_stream_offline_buffer_cap": 8192,
        },
        "models_cache": {
            "loaded": False,
            "provider_groups": 0,
            "total_models": 0,
            "age_seconds": None,
        },
    }


def test_system_health_runtime_models_cache_invalid_and_untimestamped_states(
    monkeypatch,
):
    from api import system_health

    base_sources = {
        "sessions": {},
        "sessions_lock": threading.Lock(),
        "sessions_effective_cap": 1,
        "streams": {},
        "streams_lock": threading.Lock(),
        "stream_buffer_cap": 8,
        "session_list_cache": {},
        "session_list_cache_inflight": {},
        "session_list_cache_lock": threading.Lock(),
        "session_list_cache_cap": 2,
        "is_valid_models_cache": lambda value: (
            isinstance(value, dict) and value.get("valid") is True
        ),
    }

    monkeypatch.setattr(
        system_health,
        "_webui_runtime_sources",
        lambda: {
            **base_sources,
            "models_cache_snapshot": lambda: (
                {"groups": [{"models": [{"id": "secret"}]}]},
                10.0,
            ),
        },
    )
    invalid = system_health._webui_runtime_payload()["models_cache"]
    assert invalid == {
        "loaded": False,
        "provider_groups": 0,
        "total_models": 0,
        "age_seconds": None,
    }

    monkeypatch.setattr(
        system_health,
        "_webui_runtime_sources",
        lambda: {
            **base_sources,
            "models_cache_snapshot": lambda: ({"valid": True, "groups": []}, 0.0),
        },
    )
    untimestamped = system_health._webui_runtime_payload()["models_cache"]
    assert untimestamped == {
        "loaded": True,
        "provider_groups": 0,
        "total_models": 0,
        "age_seconds": None,
    }


def test_system_health_runtime_source_failure_is_reported(monkeypatch):
    from api import system_health

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 10.0)
    monkeypatch.setattr(
        system_health,
        "_memory_usage",
        lambda: {"used_bytes": 1, "total_bytes": 2, "percent": 50.0},
    )
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 1, "total_bytes": 2, "percent": 50.0},
    )
    monkeypatch.setattr(
        system_health,
        "_webui_runtime_sources",
        lambda: (_ for _ in ()).throw(RuntimeError("private /home/user/secret-token")),
    )

    payload = system_health.build_system_health_payload()

    assert payload["status"] == "partial"
    assert payload["available"] is True
    assert payload["webui_runtime"] == system_health._zero_webui_runtime_payload()
    assert payload["errors"] == [{"metric": "webui_runtime", "code": "RuntimeError"}]
    assert "secret-token" not in repr(payload)


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
            raise AssertionError(
                "missing optional psutil should surface a safe unavailable error"
            )


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
        raise AssertionError(
            "meminfo invariant RuntimeError should not fall back to psutil"
        )


def test_system_health_cpu_second_procfs_read_fallback_does_not_sleep_twice(
    monkeypatch,
):
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
    monkeypatch.setitem(
        sys.modules, "psutil", SimpleNamespace(cpu_percent=fake_cpu_percent)
    )

    assert system_health._cpu_percent() == 12.3
    assert calls == [
        "proc",
        ("sleep", system_health._CPU_SAMPLE_SECONDS),
        "proc",
        ("psutil", 0.0),
    ]


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
        assert (
            check_auth(handler, SimpleNamespace(path="/api/system/health", query=""))
            is False
        )
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
            "webui_runtime": {
                "sessions": {"resident_count": 1, "effective_cap": 100},
                "session_list_cache": {
                    "entries": 0,
                    "entry_cap": 64,
                    "inflight_rebuilds": 0,
                },
                "streams": {
                    "active_streams": 0,
                    "total_subscribers": 0,
                    "total_offline_buffered_events": 0,
                    "total_offline_dropped_events": 0,
                    "per_stream_offline_buffer_cap": 8192,
                },
                "models_cache": {
                    "loaded": False,
                    "provider_groups": 0,
                    "total_models": 0,
                    "age_seconds": None,
                },
            },
            "errors": [],
        },
    )
    handler = _FakeHandler()
    assert (
        routes.handle_get(handler, urlparse("http://example.test/api/system/health"))
        is True
    )
    payload = handler.json_body()
    assert payload["cpu"]["percent"] == 12.0
    assert set(payload) == {
        "status",
        "available",
        "checked_at",
        "cpu",
        "memory",
        "disk",
        "webui_runtime",
        "errors",
    }


def test_system_health_panel_markup_and_styles_live_under_insights_not_top_chrome():
    top_shell = INDEX_HTML[: INDEX_HTML.index('<div class="layout">')]
    assert 'id="systemHealthPanel"' not in top_shell
    assert 'aria-label="Host resource health"' not in top_shell
    assert "function _renderSystemHealthPanel()" in PANELS_JS
    assert 'id="systemHealthPanel"' in PANELS_JS
    assert 'aria-label="Host resource health"' in PANELS_JS
    assert "System health" in PANELS_JS
    assert "Current VPS resource usage" in PANELS_JS
    assert PANELS_JS.index("_renderSystemHealthPanel()") < PANELS_JS.index(
        "_renderLlmWikiStatus(wikiStatus)"
    )
    assert 'data-system-health-metric="cpu"' in PANELS_JS
    assert 'data-system-health-metric="memory"' in PANELS_JS
    assert 'data-system-health-metric="disk"' in PANELS_JS
    assert ".system-health-panel.insights-card" in STYLE_CSS
    assert ".system-health-bar-fill" in STYLE_CSS
    assert ".system-health-panel.unavailable" in STYLE_CSS
    assert (
        "@media(max-width:640px)" in STYLE_CSS
        and ".system-health-panel.insights-card" in STYLE_CSS
    )


def test_system_health_frontend_polls_visible_and_renders_progress_labels():
    assert "const SYSTEM_HEALTH_INTERVAL_MS=5000" in UI_JS
    assert "api('/api/system/health',{timeoutToast:false})" in UI_JS
    assert "document.visibilityState !== 'visible'" in UI_JS
    assert "document.querySelector('main.main.showing-insights')" in UI_JS
    assert (
        "document.addEventListener('visibilitychange',_syncSystemHealthMonitorVisibility)"
        in UI_JS
    )
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


def test_system_health_uses_request_profile_config_without_mutating_global_config(
    monkeypatch, tmp_path
):
    from api import config
    from api import models
    from api import profiles
    from api import routes
    from api import system_health
    from api.models import new_session

    default_path = tmp_path / "default-config.yaml"
    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    work_path = work_home / "config.yaml"
    default_config = {
        "webui": {"session_save_mode": "eager", "sessions_cache_max": 101}
    }
    work_path.write_text(
        "webui:\n  session_save_mode: deferred\n  sessions_cache_max: ${PROFILE_CAP}\n",
        encoding="utf-8",
    )
    (work_home / ".env").write_text("PROFILE_CAP=202\n", encoding="utf-8")
    default_path.write_text("default", encoding="utf-8")
    expected_default_config = copy.deepcopy(default_config)
    global_cache = copy.deepcopy(default_config)

    monkeypatch.setattr(config, "_cfg_cache", global_cache)
    monkeypatch.setattr(config, "cfg", global_cache)
    monkeypatch.setattr(config, "_cfg_path", default_path)
    monkeypatch.setattr(config, "_yaml_file_cache", {})
    monkeypatch.setenv("PROFILE_CAP", "731")
    monkeypatch.setattr(
        config,
        "_get_config_path",
        lambda: work_path,
    )
    config._yaml_file_cache[str(work_path)] = (
        ("cached", 1, 1),
        {
            "webui": {
                "session_save_mode": "deferred",
                "sessions_cache_max": "${PROFILE_CAP}",
            }
        },
    )
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: work_home if name == "work" else tmp_path,
    )
    profiles.get_profile_runtime_env(work_home)
    monkeypatch.setattr(
        system_health,
        "_webui_runtime_sources",
        system_health._webui_runtime_sources,
    )

    profiles.set_request_profile("work")
    try:
        runtime = system_health._webui_runtime_payload()
    finally:
        profiles.clear_request_profile()

    assert runtime["sessions"]["effective_cap"] == 202
    assert config._cfg_path == default_path
    assert config._cfg_cache == global_cache == expected_default_config
    assert config.cfg == expected_default_config

    monkeypatch.setattr(config, "_get_config_path", lambda: default_path)
    session_dir = tmp_path / "isolated-sessions"
    session_dir.mkdir()
    index_path = session_dir / "_index.json"
    monkeypatch.setattr(models, "SESSION_DIR", session_dir)
    monkeypatch.setattr(models, "SESSION_INDEX_FILE", index_path)
    monkeypatch.setattr(config, "SESSION_INDEX_FILE", index_path, raising=False)

    session = None
    try:
        session = new_session(workspace=str(tmp_path))
        routes._prepare_chat_start_session_for_stream(
            session,
            msg="default eager checkpoint",
            attachments=[],
            workspace=str(tmp_path),
            model=session.model,
            model_provider=session.model_provider,
            stream_id="stream_default_eager",
            started_at=123.0,
        )
        saved = json.loads(session.path.read_text(encoding="utf-8"))
        assert [message["role"] for message in saved["messages"]] == ["user"]
    finally:
        if session is not None:
            with models.LOCK:
                models.SESSIONS.pop(session.session_id, None)


def test_system_health_missing_profile_env_falls_back_to_documented_cap(
    monkeypatch, tmp_path
):
    from api import config
    from api import profiles
    from api import system_health

    work_home = tmp_path / "profiles" / "work"
    work_home.mkdir(parents=True)
    work_path = work_home / "config.yaml"
    work_path.write_text(
        "webui:\n  sessions_cache_max: ${PROFILE_CAP}\n",
        encoding="utf-8",
    )
    (work_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("PROFILE_CAP", "731")
    monkeypatch.setattr(config, "_yaml_file_cache", {})
    config._yaml_file_cache[str(work_path)] = (
        ("cached", 1, 1),
        {"webui": {"sessions_cache_max": "${PROFILE_CAP}"}},
    )
    monkeypatch.setattr(config, "_get_config_path", lambda: work_path)
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: work_home if name == "work" else tmp_path,
    )
    profiles.get_profile_runtime_env(work_home)
    monkeypatch.setattr(
        system_health,
        "_webui_runtime_sources",
        system_health._webui_runtime_sources,
    )

    profiles.set_request_profile("work")
    try:
        runtime = system_health._webui_runtime_payload()
    finally:
        profiles.clear_request_profile()

    assert runtime["sessions"]["effective_cap"] == config.DEFAULT_SESSIONS_CACHE_MAX


def test_system_health_route_does_not_wait_for_busy_cap_snapshot(monkeypatch):
    from api import config
    from api import profiles
    from api import routes
    from api import system_health

    work_home = pathlib.Path(config._get_config_path()).parent
    monkeypatch.setattr(
        profiles,
        "get_hermes_home_for_profile",
        lambda name: work_home if name == "work" else work_home.parent,
    )
    monkeypatch.setattr(
        profiles,
        "_profile_home_snapshot",
        {"default": work_home.parent, "work": work_home},
    )
    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 10.0)
    monkeypatch.setattr(
        system_health,
        "_memory_usage",
        lambda: {"used_bytes": 100, "total_bytes": 200, "percent": 50.0},
    )
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 30, "total_bytes": 60, "percent": 50.0},
    )
    lock = config._sessions_cap_snapshot_lock
    handler = _FakeHandler()
    result = {}
    finished = threading.Event()

    def serve():
        profiles.set_request_profile("work")
        try:
            result["handled"] = routes.handle_get(
                handler, urlparse("http://example.test/api/system/health")
            )
            finished.set()
        finally:
            profiles.clear_request_profile()

    try:
        assert lock.acquire(blocking=False)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            assert finished.wait(0.5), "health route waited for the config cache lock"
        finally:
            lock.release()
        thread.join(timeout=2)
    finally:
        profiles.clear_request_profile()

    assert result["handled"] is True
    payload = handler.json_body()
    assert payload["status"] == "ok"
    assert payload["available"] is True
    assert payload["errors"] == []
    sessions = payload["webui_runtime"]["sessions"]
    assert sessions["effective_cap"] == config.get_sessions_cache_max({})
    assert sessions["resident_count"] == len(config.SESSIONS)


def test_system_health_route_does_not_wait_for_production_session_lock_holder(
    monkeypatch,
):
    from api import models
    from api import routes
    from api import system_health

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 10.0)
    monkeypatch.setattr(
        system_health,
        "_memory_usage",
        lambda: {"used_bytes": 100, "total_bytes": 200, "percent": 50.0},
    )
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 30, "total_bytes": 60, "percent": 50.0},
    )

    class _BlockingIndexPath:
        def __init__(self):
            self.read_started = threading.Event()
            self.release_read = threading.Event()

        def exists(self):
            return True

        def with_suffix(self, _suffix):
            return self

        def read_bytes(self):
            self.read_started.set()
            assert self.release_read.wait(1.0)
            return b"[]"

    fake_index = _BlockingIndexPath()
    handler = _FakeHandler()
    result = {}
    finished = threading.Event()

    monkeypatch.setattr(models, "SESSION_INDEX_FILE", fake_index)

    worker = threading.Thread(target=models.prune_session_from_index, args=("missing",))
    worker.start()
    assert fake_index.read_started.wait(0.5), "session-index read never started"

    def serve():
        result["handled"] = routes.handle_get(
            handler, urlparse("http://example.test/api/system/health")
        )
        finished.set()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert finished.wait(0.5), "health route waited for the session index prune"
    finally:
        fake_index.release_read.set()
    thread.join(timeout=2)
    worker.join(timeout=2)

    assert result["handled"] is True
    payload = handler.json_body()
    assert payload["status"] == "ok"
    assert payload["available"] is True
    assert payload["errors"] == []
    assert payload["webui_runtime"]["sessions"] == system_health._zero_webui_runtime_payload()[
        "sessions"
    ]


def test_system_health_route_does_not_wait_for_busy_models_cache_lock(monkeypatch):
    from api import config
    from api import routes
    from api import system_health

    lock = config._available_models_cache_lock
    handler = _FakeHandler()
    result = {}
    finished = threading.Event()

    monkeypatch.setattr(system_health, "_cpu_percent", lambda: 10.0)
    monkeypatch.setattr(
        system_health,
        "_memory_usage",
        lambda: {"used_bytes": 100, "total_bytes": 200, "percent": 50.0},
    )
    monkeypatch.setattr(
        system_health,
        "_disk_usage",
        lambda: {"used_bytes": 30, "total_bytes": 60, "percent": 50.0},
    )

    def serve():
        result["handled"] = routes.handle_get(
            handler, urlparse("http://example.test/api/system/health")
        )
        finished.set()

    assert lock.acquire(blocking=False)
    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert finished.wait(0.5), (
            "health route waited for the models-cache rebuild lock"
        )
    finally:
        lock.release()
    thread.join(timeout=2)

    assert result["handled"] is True
    payload = handler.json_body()
    assert payload["status"] == "ok"
    assert payload["available"] is True
    assert payload["errors"] == []
    assert payload["webui_runtime"]["models_cache"] == {
        "loaded": False,
        "provider_groups": 0,
        "total_models": 0,
        "age_seconds": None,
    }


def test_system_health_payload_returns_default_stream_slice_when_channel_lock_is_busy(
    monkeypatch,
):
    from api import config
    from api import system_health

    channel = config.StreamChannel()
    assert channel._lock.acquire(blocking=False)
    try:
        monkeypatch.setattr(
            system_health,
            "_webui_runtime_sources",
            lambda: {
                "sessions": {},
                "sessions_lock": threading.Lock(),
                "sessions_effective_cap": (100, True),
                "streams": {"one": channel},
                "streams_lock": threading.Lock(),
                "stream_buffer_cap": 8192,
                "session_list_cache": {},
                "session_list_cache_inflight": {},
                "session_list_cache_lock": threading.Lock(),
                "session_list_cache_cap": 64,
                "models_cache_snapshot": lambda: (None, 0.0),
                "is_valid_models_cache": lambda snapshot: False,
            },
            raising=False,
        )

        payload = system_health._webui_runtime_payload()
    finally:
        channel._lock.release()

    assert payload["streams"] == {
        "active_streams": 0,
        "total_subscribers": 0,
        "total_offline_buffered_events": 0,
        "total_offline_dropped_events": 0,
        "per_stream_offline_buffer_cap": 8192,
    }


def test_system_health_models_cache_snapshot_releases_successful_lock(monkeypatch):
    from api import config
    from api import system_health

    class _SpyLock:
        def __init__(self):
            self.acquire_calls = []
            self.release_calls = 0
            self.held = False

        def acquire(self, *, blocking=True):
            self.acquire_calls.append(blocking)
            self.held = True
            return True

        def release(self):
            self.release_calls += 1
            self.held = False

    lock = _SpyLock()
    cached_models = {"groups": [{"models": [{"id": "gpt-5.5"}]}]}
    config_path = system_health.Path("health-test-config.yaml")
    monkeypatch.setattr(config, "_available_models_cache_lock", lock)
    monkeypatch.setattr(config, "_available_models_cache", cached_models)
    monkeypatch.setattr(config, "_available_models_cache_ts", 42.0)
    monkeypatch.setattr(config, "_get_config_path", lambda: config_path)
    monkeypatch.setattr(
        config,
        "_load_yaml_config_file",
        lambda path: {"webui": {"sessions_cache_max": 202}},
    )

    sources = system_health._webui_runtime_sources()
    snapshot = sources["models_cache_snapshot"]()

    assert snapshot == (cached_models, 42.0)
    assert lock.acquire_calls == [False]
    assert lock.release_calls == 1
    assert lock.held is False


def test_sessions_cap_snapshot_is_bounded_and_scalar(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setattr(config, "_sessions_cap_snapshots", __import__("collections").OrderedDict())
    monkeypatch.setattr(config, "_sessions_cap_generations", {})
    for index in range(65):
        home = tmp_path / f"profile-{index}"
        generation = config.observe_sessions_cap_sources(home, (index, 1), None)
        config.publish_sessions_cap_snapshot(
            home, {"webui": {"sessions_cache_max": index + 1}}, generation=generation
        )
    assert len(config._sessions_cap_snapshots) == 64
    assert all(set(record) == {"generation", "cap", "process_authority"}
               for record in config._sessions_cap_snapshots.values())


def test_sessions_cap_snapshot_invalidation_uses_production_fallback(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setattr(config, "SESSIONS_MAX", 222)
    home = tmp_path / "profile"
    generation = config.observe_sessions_cap_sources(home, (1, 1), None)
    config.publish_sessions_cap_snapshot(
        home, {"webui": {"sessions_cache_max": 731}}, generation=generation
    )
    config.invalidate_sessions_cap_snapshot(home)
    assert config.try_get_sessions_cap_snapshot(home) == (222, False)


def test_profile_owned_snapshot_precedes_attached_publish(tmp_path):
    from api import config

    home = tmp_path / "profile"
    generation = config.observe_sessions_cap_sources(home, (1, 1), None)
    config.publish_sessions_cap_snapshot(home, {"webui": {"sessions_cache_max": 731}},
                                         generation=generation, process_authority="proc")
    config.publish_sessions_cap_snapshot(home, {"webui": {"sessions_cache_max": 202}},
                                         generation=generation, process_authority=None)
    assert config.try_get_sessions_cap_snapshot(home, process_authority="proc") == (202, True)
    config.publish_sessions_cap_snapshot(home, {"webui": {"sessions_cache_max": 303}},
                                         generation=generation, process_authority="proc")
    assert config.try_get_sessions_cap_snapshot(home, process_authority="proc") == (202, True)


def test_generic_yaml_loader_does_not_publish_target_stamped_snapshot(monkeypatch, tmp_path):
    from api import config

    home = tmp_path / "profile"
    path = home / "config.yaml"
    home.mkdir()
    path.write_text("webui:\n  sessions_cache_max: 731\n", encoding="utf-8")
    monkeypatch.setattr(config, "_sessions_cap_snapshots", __import__("collections").OrderedDict())
    monkeypatch.setattr(config, "_sessions_cap_generations", {})
    config._load_yaml_config_file(path)
    assert not config._sessions_cap_snapshots
    assert config.try_get_sessions_cap_snapshot(home)[1] is False


def test_health_profile_home_lookup_does_not_spawn_on_cold_root_alias(monkeypatch):
    from api import profiles

    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", False)
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: (_ for _ in ()).throw(AssertionError()))
    home = profiles.get_cached_profile_home_for_diagnostics("renamed-root")
    assert home is None


def test_general_profile_resolution_keeps_cold_renamed_root_semantics(monkeypatch):
    from api import profiles

    monkeypatch.setattr(profiles, "_root_profile_name_cache_loaded", False)
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "renamed-root", "is_default": True}],
    )
    assert profiles.get_hermes_home_for_profile("renamed-root") == profiles._DEFAULT_HERMES_HOME


def test_sessions_cap_generation_bound_applies_to_invalidation(monkeypatch, tmp_path):
    from api import config

    monkeypatch.setattr(config, "_sessions_cap_snapshots", __import__("collections").OrderedDict())
    monkeypatch.setattr(config, "_sessions_cap_generations", {})
    for index in range(65):
        config.invalidate_sessions_cap_snapshot(tmp_path / f"profile-{index}")
    assert len(config._sessions_cap_generations) == 64


def test_reload_config_publishes_current_process_snapshot(monkeypatch, tmp_path):
    from api import config

    home = tmp_path / "startup"
    home.mkdir()
    path = home / "config.yaml"
    path.write_text("webui:\n  sessions_cache_max: 731\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(config, "_get_config_path", lambda: path)
    monkeypatch.setattr(config, "_sessions_cap_snapshots", __import__("collections").OrderedDict())
    monkeypatch.setattr(config, "_sessions_cap_generations", {})

    config.reload_config()

    assert config.try_get_sessions_cap_snapshot(home) == (731, True)


def test_per_client_switch_publishes_profile_cap_without_global_reload(monkeypatch, tmp_path):
    from api import config
    from api import profiles

    home = tmp_path / "work"
    home.mkdir()
    (home / "config.yaml").write_text(
        "webui:\n  sessions_cache_max: 731\n", encoding="utf-8"
    )
    monkeypatch.setattr(config, "_sessions_cap_snapshots", __import__("collections").OrderedDict())
    monkeypatch.setattr(config, "_sessions_cap_generations", {})
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: False)
    monkeypatch.setattr(profiles, "_is_root_profile", lambda name: name == "default")
    monkeypatch.setattr(profiles, "_resolve_named_profile_home", lambda name: home)
    monkeypatch.setattr(profiles, "_active_profile", "default")

    profiles.switch_profile("work", process_wide=False)

    assert config.try_get_sessions_cap_snapshot(home) == (731, True)
    assert profiles.get_cached_profile_home_for_diagnostics("work") == home


def test_delete_boundary_removes_identity_and_requires_republish(monkeypatch, tmp_path):
    from api import config
    from api import profiles

    home = tmp_path / "work"
    monkeypatch.setattr(profiles, "_profile_home_snapshot", {"work": home})
    generation = config.observe_sessions_cap_sources(home, (1, 1), None)
    config.publish_sessions_cap_snapshot(
        home, {"webui": {"sessions_cache_max": 731}}, generation=generation
    )
    profiles._forget_profile_home("work", home)
    config.invalidate_sessions_cap_snapshot(home)

    assert profiles.get_cached_profile_home_for_diagnostics("work") is None
    assert config.try_get_sessions_cap_snapshot(home)[1] is False

    profiles._remember_profile_home("work", home)
    generation = config.observe_sessions_cap_sources(home, (2, 1), None)
    config.publish_sessions_cap_snapshot(
        home, {"webui": {"sessions_cache_max": 202}}, generation=generation
    )
    assert config.try_get_sessions_cap_snapshot(home) == (202, True)


def test_renamed_root_alias_health_consumer_reads_configured_root_cap(monkeypatch):
    from api import config
    from api import profiles
    from api import system_health

    root = profiles._DEFAULT_HERMES_HOME
    monkeypatch.setattr(profiles, "_profile_home_snapshot", {"renamed-root": root})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "renamed-root")
    monkeypatch.setattr(config, "_sessions_cap_snapshots", __import__("collections").OrderedDict())
    monkeypatch.setattr(config, "_sessions_cap_generations", {})
    generation = config.observe_sessions_cap_sources(root, (9, 1), None)
    config.publish_sessions_cap_snapshot(
        root, {"webui": {"sessions_cache_max": 731}}, generation=generation,
        process_authority=None,
    )

    assert profiles.get_cached_profile_home_for_diagnostics("renamed-root") == root
    assert system_health._cached_profile_sessions_cache_cap(config) == (731, True)


def test_isolated_startup_seeds_identity_before_first_health_read(monkeypatch, tmp_path):
    from api import config
    from api import profiles
    from api import system_health

    home = tmp_path / "isolated"
    home.mkdir()
    monkeypatch.setattr(profiles, "_INITIAL_HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "_is_isolated_profile_mode", lambda: True)
    monkeypatch.setattr(profiles, "_isolated_profile_name", lambda: "isolated")
    monkeypatch.setattr(profiles, "_set_hermes_home", lambda _home: None)
    monkeypatch.setattr(profiles, "install_cron_scheduler_profile_isolation", lambda: None)
    monkeypatch.setattr(profiles, "_reload_dotenv", lambda _home: None)
    monkeypatch.setattr(config, "_sessions_cap_snapshots", __import__("collections").OrderedDict())
    monkeypatch.setattr(config, "_sessions_cap_generations", {})
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "isolated")

    profiles.init_profile_state()
    generation = config.observe_sessions_cap_sources(home, (1, 1), None)
    config.publish_sessions_cap_snapshot(
        home, {"webui": {"sessions_cache_max": 731}}, generation=generation,
        process_authority=None,
    )

    assert profiles.get_cached_profile_home_for_diagnostics("isolated") == home.resolve()
    assert system_health._cached_profile_sessions_cache_cap(config) == (731, True)
