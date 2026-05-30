"""Coverage for WebUI same-thread cron origin delivery."""

import io
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
PANELS_JS = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")


class _JSONHandler:
    def __init__(self, headers=None):
        self.status = None
        self.response_headers = []
        self.headers = headers or {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass


def _payload(handler):
    return json.loads(handler.wfile.getvalue().decode("utf-8"))


def _js_function_body(name: str) -> str:
    marker = f"function {name}("
    start = PANELS_JS.find(marker)
    assert start != -1, f"{name} not found"
    paren = PANELS_JS.find("(", start)
    assert paren != -1, f"{name} params not found"
    depth = 0
    for idx in range(paren, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = PANELS_JS.find("{", idx)
                break
    else:
        raise AssertionError(f"{name} params did not terminate")
    assert brace != -1, f"{name} body not found"
    depth = 0
    for idx in range(brace, len(PANELS_JS)):
        ch = PANELS_JS[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return PANELS_JS[brace + 1 : idx]
    raise AssertionError(f"{name} body did not terminate")


def _install_cron_fakes(monkeypatch, calls, created=None, deliver_result=None):
    cron_pkg = types.ModuleType("cron")
    cron_pkg.__path__ = []
    cron_jobs = types.ModuleType("cron.jobs")
    cron_scheduler = types.ModuleType("cron.scheduler")
    created = created or {"id": "job-origin", "name": "Origin job"}

    cron_jobs.create_job = lambda **kwargs: calls.append(("create", kwargs)) or {**created, **kwargs}
    cron_jobs.update_job = lambda job_id, updates: calls.append(("update", job_id, updates)) or {
        **created,
        **updates,
    }
    cron_jobs.save_job_output = lambda job_id, output: calls.append(("save", job_id, output))
    cron_jobs.mark_job_run = lambda job_id, success, error=None, delivery_error=None: calls.append(
        ("mark", job_id, success, error, delivery_error)
    )
    if deliver_result is None:
        deliver_result = lambda job, content: calls.append(("agent-deliver", job["id"], content)) or None
    cron_scheduler.SILENT_MARKER = "[SILENT]"
    cron_scheduler._deliver_result = deliver_result

    monkeypatch.setitem(sys.modules, "cron", cron_pkg)
    monkeypatch.setitem(sys.modules, "cron.jobs", cron_jobs)
    monkeypatch.setitem(sys.modules, "cron.scheduler", cron_scheduler)


def test_save_cron_form_posts_explicit_origin_session_id_for_same_thread_delivery():
    body = _js_function_body("saveCronForm")
    assert "const activeSessionId = S.session&&S.session.session_id ? String(S.session.session_id).trim() : '';" in body
    assert "if (deliver === 'origin' && activeSessionId) updates.origin_session_id = activeSessionId;" in body
    assert "if(deliver==='origin'&&activeSessionId)body.origin_session_id=activeSessionId;" in body


def test_cron_create_persists_webui_origin_for_origin_delivery(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(session_id=sid),
    )

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "name": "Same thread",
            "prompt": "report back",
            "schedule": "every 60m",
            "deliver": "origin",
            "origin_session_id": "webui_origin_001",
        },
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body["ok"] is True
    assert calls[0][0] == "create"
    assert calls[0][1]["deliver"] == "origin"
    assert calls[0][1]["origin"] == {
        "platform": "webui",
        "chat_id": "webui_origin_001",
        "session_id": "webui_origin_001",
    }


def test_cron_create_does_not_infer_origin_for_local_delivery_referer(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(session_id=sid),
    )

    handler = _JSONHandler(headers={"Referer": "http://127.0.0.1/session/webui_origin_001"})
    routes._handle_cron_create(
        handler,
        {
            "name": "Local job",
            "prompt": "save only",
            "schedule": "every 60m",
            "deliver": "local",
        },
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body["ok"] is True
    assert calls[0][1]["deliver"] == "local"
    assert calls[0][1]["origin"] is None


def test_cron_create_persists_profile_scoped_webui_origin(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(routes, "_available_cron_profile_names", lambda: {"default", "ops"})
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(session_id=sid, profile="ops"),
    )

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "name": "Profile thread",
            "prompt": "report back",
            "schedule": "every 60m",
            "deliver": "origin",
            "origin_session_id": "webui_origin_ops",
            "profile": "ops",
        },
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body["ok"] is True
    assert calls[0][1]["origin"] == {
        "platform": "webui",
        "chat_id": "webui_origin_ops",
        "session_id": "webui_origin_ops",
        "profile": "ops",
    }


def test_cron_create_rejects_cross_profile_webui_origin(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(routes, "_available_cron_profile_names", lambda: {"default", "ops"})
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(session_id=sid, profile="default"),
    )

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "prompt": "report back",
            "schedule": "every 60m",
            "deliver": "origin",
            "origin_session_id": "webui_origin_ops",
            "profile": "ops",
        },
    )

    body = _payload(handler)
    assert handler.status == 400
    assert "webui origin session profile mismatch" in body["error"]
    assert not calls


def test_cron_update_persists_webui_origin_for_origin_delivery(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(session_id=sid),
    )

    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {
            "job_id": "job-origin",
            "deliver": "origin",
            "origin_session_id": "webui_origin_002",
        },
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body["ok"] is True
    assert calls == [
        (
            "update",
            "job-origin",
            {
                "deliver": "origin",
                "origin": {
                    "platform": "webui",
                    "chat_id": "webui_origin_002",
                    "session_id": "webui_origin_002",
                },
            },
        )
    ]


def test_cron_update_rejects_cross_profile_webui_origin_before_update(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(routes, "_available_cron_profile_names", lambda: {"default", "ops"})
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid, metadata_only=False: SimpleNamespace(session_id=sid, profile="default"),
    )

    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {
            "job_id": "job-origin-profile",
            "deliver": "origin",
            "origin_session_id": "webui_origin_ops",
            "profile": "ops",
        },
    )

    body = _payload(handler)
    assert handler.status == 400
    assert "webui origin session profile mismatch" in body["error"]
    assert not calls


def test_cron_update_rejects_missing_webui_origin_before_update(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)

    def missing_session(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(routes, "get_session", missing_session)

    handler = _JSONHandler()
    routes._handle_cron_update(
        handler,
        {
            "job_id": "job-origin-missing",
            "deliver": "origin",
            "origin_session_id": "missing_session",
        },
    )

    body = _payload(handler)
    assert handler.status == 400
    assert "WebUI origin session not found" in body["error"]
    assert not calls


def test_cron_update_clears_webui_origin_for_local_delivery(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    loads = []

    def get_origin_session(sid, metadata_only=False):
        loads.append((sid, metadata_only))
        return SimpleNamespace(session_id=sid)

    monkeypatch.setattr(
        routes,
        "get_session",
        get_origin_session,
    )

    handler = _JSONHandler(headers={"Referer": "http://127.0.0.1/session/webui_origin_002"})
    routes._handle_cron_update(
        handler,
        {
            "job_id": "job-local",
            "deliver": "local",
            "origin_session_id": "webui_origin_002",
        },
    )

    body = _payload(handler)
    assert handler.status == 200
    assert body["ok"] is True
    assert calls == [("update", "job-local", {"deliver": "local", "origin": None})]
    assert loads == []


def test_manual_cron_run_appends_webui_origin_result_to_same_session(monkeypatch):
    import api.routes as routes

    calls = []
    saved = []
    session = SimpleNamespace(messages=[], save=lambda: saved.append(True))
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "raw output", "final response", None),
    )
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)

    routes._mark_cron_running("job-origin")
    routes._run_cron_tracked(
        {
            "id": "job-origin",
            "name": "Nightly summary",
            "deliver": "origin",
            "origin": {
                "platform": "webui",
                "chat_id": "webui_origin_001",
                "session_id": "webui_origin_001",
            },
        }
    )

    assert ("agent-deliver", "job-origin", "final response") not in calls
    assert calls == [
        ("save", "job-origin", "raw output"),
        ("mark", "job-origin", True, None, None),
    ]
    assert saved == [True]
    assert session.messages[-1]["role"] == "assistant"
    assert session.messages[-1]["type"] == "cron_delivery"
    assert session.messages[-1]["cron_job_id"] == "job-origin"
    assert "Cronjob Response: Nightly summary" in session.messages[-1]["content"]
    assert "final response" in session.messages[-1]["content"]


def test_manual_cron_run_ignores_stale_webui_origin_for_local_delivery(monkeypatch):
    import api.routes as routes

    calls = []
    saved = []
    session = SimpleNamespace(messages=[], save=lambda: saved.append(True))
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "raw output", "final response", None),
    )
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)

    routes._mark_cron_running("job-local-stale-origin")
    routes._run_cron_tracked(
        {
            "id": "job-local-stale-origin",
            "name": "Local summary",
            "deliver": "local",
            "origin": {
                "platform": "webui",
                "chat_id": "webui_origin_001",
                "session_id": "webui_origin_001",
            },
        }
    )

    assert calls == [
        ("save", "job-local-stale-origin", "raw output"),
        ("agent-deliver", "job-local-stale-origin", "final response"),
        ("mark", "job-local-stale-origin", True, None, None),
    ]
    assert saved == []
    assert session.messages == []


def test_manual_cron_run_uses_profile_scoped_webui_origin_session(monkeypatch):
    import api.routes as routes

    calls = []
    loads = []
    saved = []
    session = SimpleNamespace(profile="ops", messages=[], save=lambda: saved.append(True))
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "raw output", "profile response", None),
    )

    def get_profile_session(sid, metadata_only=False):
        loads.append((sid, metadata_only))
        return session

    monkeypatch.setattr(routes, "get_session", get_profile_session)

    routes._mark_cron_running("job-origin-profile")
    routes._run_cron_tracked(
        {
            "id": "job-origin-profile",
            "name": "Profile summary",
            "deliver": "origin",
            "origin": {
                "platform": "webui",
                "chat_id": "webui_origin_ops",
                "session_id": "webui_origin_ops",
                "profile": "ops",
            },
        }
    )

    assert loads == [("webui_origin_ops", False)]
    assert saved == [True]
    assert session.messages[-1]["cron_job_id"] == "job-origin-profile"
    assert "profile response" in session.messages[-1]["content"]
    assert calls == [
        ("save", "job-origin-profile", "raw output"),
        ("mark", "job-origin-profile", True, None, None),
    ]


def test_manual_cron_run_records_profile_mismatch_delivery_error(monkeypatch):
    import api.routes as routes

    calls = []
    session = SimpleNamespace(profile="default", messages=[], save=lambda: None)
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "raw output", "final response", None),
    )
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)

    routes._mark_cron_running("job-origin-profile-mismatch")
    routes._run_cron_tracked(
        {
            "id": "job-origin-profile-mismatch",
            "deliver": "origin",
            "origin": {
                "platform": "webui",
                "chat_id": "webui_origin_ops",
                "session_id": "webui_origin_ops",
                "profile": "ops",
            },
        }
    )

    assert calls == [
        ("save", "job-origin-profile-mismatch", "raw output"),
        (
            "mark",
            "job-origin-profile-mismatch",
            True,
            None,
            "webui origin session profile mismatch: expected ops, found default",
        ),
    ]
    assert session.messages == []


def test_manual_cron_run_records_missing_webui_origin_delivery_error(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)
    monkeypatch.setattr(
        routes,
        "_run_cron_job_in_profile_subprocess",
        lambda job, execution_profile_home: (True, "raw output", "final response", None),
    )

    def missing_session(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(routes, "get_session", missing_session)

    routes._mark_cron_running("job-origin-missing")
    routes._run_cron_tracked(
        {
            "id": "job-origin-missing",
            "deliver": "origin",
            "origin": {
                "platform": "webui",
                "chat_id": "missing_session",
                "session_id": "missing_session",
            },
        }
    )

    assert calls == [
        ("save", "job-origin-missing", "raw output"),
        (
            "mark",
            "job-origin-missing",
            True,
            None,
            "webui origin session not found: missing_session",
        ),
    ]


def test_webui_origin_rejects_unknown_session_before_create(monkeypatch):
    import api.routes as routes

    calls = []
    _install_cron_fakes(monkeypatch, calls)

    def missing_session(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(routes, "get_session", missing_session)

    handler = _JSONHandler()
    routes._handle_cron_create(
        handler,
        {
            "prompt": "report back",
            "schedule": "every 60m",
            "deliver": "origin",
            "origin_session_id": "missing_session",
        },
    )

    body = _payload(handler)
    assert handler.status == 400
    assert "WebUI origin session not found" in body["error"]
    assert not calls
