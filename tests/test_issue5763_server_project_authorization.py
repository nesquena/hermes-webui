"""Server-side project authorization for POST /api/session/new (#5763)."""

from __future__ import annotations

import json
import tempfile
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

import api.routes as routes

_GENERIC_ERROR = "Invalid project assignment"


class _Session:
    session_id = "created-session"
    messages: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.profile = kwargs.get("profile")
        self.project_id = kwargs.get("project_id")

    def compact(self):
        return {
            "session_id": self.session_id,
            "profile": self.profile,
            "project_id": self.project_id,
        }


def _post_session_new(monkeypatch, body, *, projects=(), active_profile="default"):
    calls = {
        "load_projects": 0,
        "workspace": 0,
        "worktree": 0,
        "memory_visibility": 0,
        "new_session": 0,
        "success_response": 0,
    }
    captured = {}

    def load_projects():
        calls["load_projects"] += 1
        if isinstance(projects, BaseException):
            raise projects
        return projects

    def resolve_workspace(*_args, **_kwargs):
        calls["workspace"] += 1
        return "/trusted/workspace"

    def create_worktree(*_args, **_kwargs):
        calls["worktree"] += 1
        return {
            "path": "/trusted/worktree",
            "branch": "test",
            "repo_root": "/trusted/workspace",
            "created_at": 1,
        }

    def check_prev_session(*_args, **_kwargs):
        calls["memory_visibility"] += 1
        return True

    def new_session(**kwargs):
        calls["new_session"] += 1
        captured["new_session_kwargs"] = kwargs
        return _Session(**kwargs)

    def success_response(_handler, payload, status=200, **_kwargs):
        calls["success_response"] += 1
        captured["success"] = (status, payload)
        return True

    def error_response(_handler, message, status=400, **_kwargs):
        captured["error"] = (status, message)
        return True

    monkeypatch.setattr(routes, "read_body", lambda _handler: body)
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(routes, "_csrf_exempt_path", lambda _path: False)
    monkeypatch.setattr(
        routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: active_profile)
    monkeypatch.setattr(routes, "load_projects", load_projects)
    monkeypatch.setattr(routes, "_resolve_new_session_workspace", resolve_workspace)
    monkeypatch.setattr(routes, "_worktree_default_from_config", lambda _profile: False)
    monkeypatch.setattr(routes, "_session_id_visible_to_request_profile", check_prev_session)
    monkeypatch.setattr(routes, "new_session", new_session)
    monkeypatch.setattr(routes, "j", success_response)
    monkeypatch.setattr(routes, "bad", error_response)

    import api.worktrees as worktrees

    monkeypatch.setattr(worktrees, "create_worktree_for_workspace", create_worktree)

    handler = SimpleNamespace(command="POST", headers={})
    assert routes.handle_post(handler, urlparse("/api/session/new")) is True
    return captured, calls


def _assert_rejected_before_side_effects(captured, calls):
    assert captured == {"error": (400, _GENERIC_ERROR)}
    assert calls["workspace"] == 0
    assert calls["worktree"] == 0
    assert calls["memory_visibility"] == 0
    assert calls["new_session"] == 0
    assert calls["success_response"] == 0


@pytest.mark.parametrize("read_only", [pytest.param(False, id="literal-false"), pytest.param("absent", id="absent")])
def test_session_new_accepts_active_writable_legacy_project(monkeypatch, read_only):
    row = {"project_id": "legacy-project", "profile": "work"}
    if read_only != "absent":
        row["read_only"] = read_only

    captured, calls = _post_session_new(
        monkeypatch,
        {"project_id": "legacy-project", "profile": "work"},
        projects=[row],
        active_profile="work",
    )

    assert "error" not in captured
    assert calls["load_projects"] == 1
    assert calls["new_session"] == 1
    assert captured["new_session_kwargs"]["project_id"] == "legacy-project"
    assert captured["success"][0] == 200


@pytest.mark.parametrize("body", [{}, {"project_id": None}], ids=["absent", "explicit-null"])
def test_session_new_preserves_unassigned_compatibility_without_loading_projects(
    monkeypatch, body
):
    captured, calls = _post_session_new(
        monkeypatch,
        body,
        projects=AssertionError("unassigned requests must not load projects"),
    )

    assert "error" not in captured
    assert calls["load_projects"] == 0
    assert captured["new_session_kwargs"]["project_id"] is None


@pytest.mark.parametrize("profile", [None, "", " padded ", 7])
def test_explicit_null_project_does_not_broaden_profile_validation(
    monkeypatch, profile
):
    captured, calls = _post_session_new(
        monkeypatch,
        {"project_id": None, "profile": profile},
        projects=AssertionError("unassigned requests must not load projects"),
    )

    assert "error" not in captured
    assert calls["load_projects"] == 0
    assert captured["new_session_kwargs"]["project_id"] is None


@pytest.mark.parametrize(
    "project_id",
    [
        pytest.param(" padded ", id="padded"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(7, id="integer"),
        pytest.param([], id="list"),
        pytest.param({}, id="object"),
    ],
)
def test_session_new_rejects_noncanonical_project_id_before_side_effects(
    monkeypatch, project_id
):
    captured, calls = _post_session_new(monkeypatch, {"project_id": project_id})

    _assert_rejected_before_side_effects(captured, calls)
    assert calls["load_projects"] == 0


@pytest.mark.parametrize(
    "projects",
    [
        pytest.param([], id="unknown-or-native-only"),
        pytest.param(
            [{"project_id": "candidate", "profile": "other"}], id="foreign-profile"
        ),
        pytest.param(
            [
                {
                    "project_id": "candidate",
                    "profile": "default",
                    "project_source": "hermes-agent",
                }
            ],
            id="native-source",
        ),
        pytest.param(
            [{"project_id": "candidate", "profile": "default", "read_only": True}],
            id="read-only",
        ),
    ],
)
def test_session_new_rejects_nonwritable_or_unknown_project_before_side_effects(
    monkeypatch, projects
):
    captured, calls = _post_session_new(
        monkeypatch,
        {"project_id": "candidate", "worktree": True, "prev_session_id": "old"},
        projects=projects,
    )

    _assert_rejected_before_side_effects(captured, calls)
    assert calls["load_projects"] == 1


@pytest.mark.parametrize(
    "read_only",
    [
        pytest.param(None, id="null"),
        pytest.param("false", id="string"),
        pytest.param(0, id="number"),
        pytest.param({}, id="object"),
    ],
)
def test_session_new_rejects_malformed_read_only_capability(monkeypatch, read_only):
    captured, calls = _post_session_new(
        monkeypatch,
        {"project_id": "candidate"},
        projects=[
            {
                "project_id": "candidate",
                "profile": "default",
                "read_only": read_only,
            }
        ],
    )

    _assert_rejected_before_side_effects(captured, calls)


@pytest.mark.parametrize(
    "profile",
    [
        pytest.param("other", id="foreign"),
        pytest.param(" work ", id="padded"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(None, id="null"),
        pytest.param(7, id="non-string"),
    ],
)
def test_project_bearing_session_new_rejects_invalid_or_foreign_body_profile(
    monkeypatch, profile
):
    captured, calls = _post_session_new(
        monkeypatch,
        {"project_id": "candidate", "profile": profile},
        projects=[{"project_id": "candidate", "profile": "work"}],
        active_profile="work",
    )

    _assert_rejected_before_side_effects(captured, calls)
    assert calls["load_projects"] == 0


def test_session_new_accepts_root_alias_for_body_and_legacy_row(monkeypatch):
    root_aliases = {"default", "kinni"}
    monkeypatch.setattr(
        routes,
        "_profiles_match",
        lambda left, right: (left == right) or ({left, right} <= root_aliases),
    )

    captured, calls = _post_session_new(
        monkeypatch,
        {"project_id": "root-project", "profile": "default"},
        projects=[{"project_id": "root-project", "profile": "kinni"}],
        active_profile="kinni",
    )

    assert "error" not in captured
    assert calls["new_session"] == 1
    assert captured["new_session_kwargs"]["project_id"] == "root-project"


def test_legacy_project_remains_writable_without_consulting_native_collision(
    monkeypatch,
):
    native_calls = []
    colliding_native = {
        "project_id": "shared",
        "profile": "work",
        "project_source": "hermes-agent",
        "read_only": True,
    }
    monkeypatch.setattr(
        routes,
        "load_native_projects",
        lambda *_args, **_kwargs: native_calls.append(True) or [colliding_native],
    )

    captured, calls = _post_session_new(
        monkeypatch,
        {"project_id": "shared", "profile": "work"},
        projects=[{"project_id": "shared", "profile": "work"}],
        active_profile="work",
    )

    assert "error" not in captured
    assert native_calls == []
    assert calls["new_session"] == 1
    assert captured["new_session_kwargs"]["project_id"] == "shared"


@pytest.mark.parametrize(
    "projects",
    [
        pytest.param(OSError("sensitive /project/path"), id="loader-error"),
        pytest.param(None, id="null-result"),
        pytest.param({}, id="object-result"),
        pytest.param(
            [{"project_id": "candidate", "profile": None}],
            id="malformed-matching-profile",
        ),
        pytest.param(
            [{"project_id": "candidate", "profile": 7}],
            id="nonstring-matching-profile",
        ),
    ],
)
def test_session_new_fails_closed_on_project_loader_or_matching_row_malformation(
    monkeypatch, projects
):
    captured, calls = _post_session_new(
        monkeypatch, {"project_id": "candidate"}, projects=projects
    )

    _assert_rejected_before_side_effects(captured, calls)
    assert captured["error"] == (400, _GENERIC_ERROR)


class _DirectPostHandler:
    command = "POST"

    def __init__(self, body):
        raw = json.dumps(body).encode("utf-8")
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = tempfile.SpooledTemporaryFile()
        self.rfile.write(raw)
        self.rfile.seek(0)
        self.wfile = tempfile.SpooledTemporaryFile()
        self.status = None
        self.client_address = ("127.0.0.1", 12345)

    def send_response(self, status):
        self.status = status

    def send_header(self, _key, _value):
        pass

    def end_headers(self):
        pass

    def payload(self):
        self.wfile.seek(0)
        return json.loads(self.wfile.read().decode("utf-8"))


def test_direct_post_rejects_forged_foreign_project_without_workspace_resolution(
    monkeypatch,
):
    handler = _DirectPostHandler(
        {"project_id": "foreign-project", "profile": "other", "workspace": "/forged"}
    )
    monkeypatch.setattr(routes, "_check_csrf", lambda _handler: True)
    monkeypatch.setattr(
        routes, "_guard_request_session_visibility", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(routes, "_get_active_profile_name", lambda: "work")
    monkeypatch.setattr(
        routes,
        "load_projects",
        lambda: [{"project_id": "foreign-project", "profile": "other"}],
    )
    monkeypatch.setattr(
        routes,
        "_resolve_new_session_workspace",
        lambda *_args, **_kwargs: pytest.fail("workspace resolution must not run"),
    )
    monkeypatch.setattr(
        routes,
        "new_session",
        lambda **_kwargs: pytest.fail("session creation must not run"),
    )

    routes.handle_post(handler, urlparse("/api/session/new"))

    assert handler.status == 400
    assert handler.payload() == {"error": _GENERIC_ERROR}
