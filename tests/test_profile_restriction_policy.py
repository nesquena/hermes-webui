import json
from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.requires_agent_modules


class _Handler:
    def __init__(self, path, body=None):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.status = None
        self.response_headers = []
        payload = json.dumps(body or {}).encode("utf-8")
        self.rfile = BytesIO(payload)
        self.wfile = BytesIO()
        self.headers["Content-Length"] = str(len(payload))

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.response_headers.append((key, value))

    def end_headers(self):
        pass

    def log_message(self, *args, **kwargs):
        pass

    @property
    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _profile_rows():
    return [
        {"name": "default", "path": "/tmp/hermes", "is_default": True, "is_active": True},
        {"name": "macos", "path": "/tmp/hermes/profiles/macos", "is_default": False, "is_active": False},
        {"name": "joey", "path": "/tmp/hermes/profiles/joey", "is_default": False, "is_active": False},
    ]


def _lock_to_macos(monkeypatch):
    monkeypatch.setenv("HERMES_WEBUI_PROFILE_LOCK", "macos")
    monkeypatch.setenv("HERMES_WEBUI_PROFILE_ALLOWLIST", "macos")


def _allowlist_default_and_macos(monkeypatch):
    monkeypatch.delenv("HERMES_WEBUI_PROFILE_LOCK", raising=False)
    monkeypatch.setenv("HERMES_WEBUI_PROFILE_ALLOWLIST", "default,macos")


def test_locked_profile_filters_profiles_response(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    _lock_to_macos(monkeypatch)
    monkeypatch.setattr(profiles, "list_profiles_api", _profile_rows)

    handler = _Handler("/api/profiles")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    data = handler.response_json
    assert data["active"] == "macos"
    assert data["profile_locked"] is True
    assert data["profile_policy_mode"] == "locked"
    assert data["locked_profile"] == "macos"
    assert [row["name"] for row in data["profiles"]] == ["macos"]
    assert data["profiles"][0]["is_active"] is True


def test_allowlist_profile_filters_without_locking_switching(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes

    _allowlist_default_and_macos(monkeypatch)
    monkeypatch.setattr(profiles, "list_profiles_api", _profile_rows)

    handler = _Handler("/api/profiles")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 200
    data = handler.response_json
    assert data["active"] == "default"
    assert data["profile_locked"] is True
    assert data["profile_policy_mode"] == "allowlist"
    assert "locked_profile" not in data
    assert data["allowed_profiles"] == ["default", "macos"]
    assert [row["name"] for row in data["profiles"]] == ["default", "macos"]


def test_locked_profile_active_ignores_request_cookie_profile(monkeypatch, tmp_path):
    import api.profiles as profiles
    import api.routes as routes

    _lock_to_macos(monkeypatch)
    base = tmp_path / ".hermes"
    macos_home = base / "profiles" / "macos"
    macos_home.mkdir(parents=True)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    profiles.set_request_profile("default")
    try:
        handler = _Handler("/api/profile/active")
        routes.handle_get(handler, urlparse(handler.path))
    finally:
        profiles.clear_request_profile()

    assert handler.status == 200
    data = handler.response_json
    assert data["name"] == "macos"
    assert data["path"] == str(macos_home)
    assert data["profile_locked"] is True
    assert data["locked_profile"] == "macos"


def test_locked_profile_switch_to_other_profile_is_forbidden(monkeypatch):
    import api.routes as routes

    _lock_to_macos(monkeypatch)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    handler = _Handler("/api/profile/switch", {"name": "default"})
    routes.handle_post(handler, SimpleNamespace(path="/api/profile/switch"))

    assert handler.status == 403
    assert "not allowed" in handler.response_json["error"].lower()


def test_allowlist_profile_switch_to_allowed_profile_is_permitted(monkeypatch):
    import api.profiles as profiles
    import api.routes as routes
    import api.helpers as helpers

    _allowlist_default_and_macos(monkeypatch)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(helpers, "build_profile_cookie", lambda name, handler=None: f"hermes_profile={name}")
    monkeypatch.setattr(profiles, "list_profiles_api", _profile_rows)
    monkeypatch.setattr(
        profiles,
        "switch_profile",
        lambda name, process_wide=False: {
            "active": name,
            "is_default": name == "default",
            "profiles": _profile_rows(),
        },
    )

    handler = _Handler("/api/profile/switch", {"name": "macos"})
    routes.handle_post(handler, SimpleNamespace(path="/api/profile/switch"))

    assert handler.status == 200
    data = handler.response_json
    assert data["active"] == "macos"
    assert data["profile_locked"] is True
    assert data["profile_policy_mode"] == "allowlist"
    assert [row["name"] for row in data["profiles"]] == ["default", "macos"]


def test_allowlist_profile_switch_to_disallowed_profile_is_forbidden(monkeypatch):
    import api.routes as routes

    _allowlist_default_and_macos(monkeypatch)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)

    handler = _Handler("/api/profile/switch", {"name": "joey"})
    routes.handle_post(handler, SimpleNamespace(path="/api/profile/switch"))

    assert handler.status == 403
    assert "not allowed" in handler.response_json["error"].lower()


def test_locked_profile_hides_foreign_session_detail_by_id(monkeypatch):
    import api.routes as routes

    _lock_to_macos(monkeypatch)
    foreign = SimpleNamespace(session_id="foreign", profile="joey")
    monkeypatch.setattr(routes, "get_session", lambda *a, **k: foreign)

    handler = _Handler("/api/session?session_id=foreign")
    routes.handle_get(handler, urlparse(handler.path))

    assert handler.status == 404
    assert handler.response_json["error"] == "Session not found"


def test_locked_profile_hides_foreign_session_export_by_id(monkeypatch):
    import api.routes as routes

    _lock_to_macos(monkeypatch)
    foreign = SimpleNamespace(session_id="foreign", profile="joey")
    monkeypatch.setattr(routes, "get_session", lambda *a, **k: foreign)

    handler = _Handler("/api/session/export?session_id=foreign")
    routes._handle_session_export(handler, urlparse(handler.path))

    assert handler.status == 404
    assert handler.response_json["error"] == "Session not found"


@pytest.mark.parametrize("path,handler_name", [
    ("/api/chat/start", "_handle_chat_start"),
    ("/api/goal", "_handle_goal_command"),
])
def test_locked_profile_blocks_foreign_session_execution_by_id(monkeypatch, path, handler_name):
    import api.routes as routes

    _lock_to_macos(monkeypatch)
    foreign = SimpleNamespace(session_id="foreign", profile="joey")
    monkeypatch.setattr(routes, "get_session", lambda *a, **k: foreign)

    body = {"session_id": "foreign", "message": "hello", "args": "go"}
    handler = _Handler(path, body)
    getattr(routes, handler_name)(handler, body)

    assert handler.status == 404
    assert handler.response_json["error"] == "Session not found"


def test_locked_profile_blocks_foreign_session_mutation_loader(monkeypatch):
    import api.routes as routes

    _lock_to_macos(monkeypatch)
    foreign = SimpleNamespace(session_id="foreign", profile="joey", read_only=False)
    monkeypatch.setattr(routes, "get_session", lambda *a, **k: foreign)
    monkeypatch.setattr(routes, "_ensure_full_session_before_mutation", lambda sid, s: s)

    with pytest.raises(KeyError):
        routes._get_or_materialize_session("foreign")


def test_locked_profile_disables_all_profiles_query_flag(monkeypatch):
    import api.routes as routes

    parsed = urlparse("/api/sessions?all_profiles=1")
    monkeypatch.delenv("HERMES_WEBUI_PROFILE_LOCK", raising=False)
    monkeypatch.delenv("HERMES_WEBUI_PROFILE_ALLOWLIST", raising=False)
    assert routes._all_profiles_query_flag(parsed) is True

    _lock_to_macos(monkeypatch)
    assert routes._all_profiles_query_flag(parsed) is False


@pytest.mark.parametrize("path,payload", [
    ("/api/profile/create", {"name": "other"}),
    ("/api/profile/delete", {"name": "joey"}),
])
def test_locked_profile_blocks_profile_management_mutations(monkeypatch, path, payload):
    import api.profiles as profiles
    import api.routes as routes

    _lock_to_macos(monkeypatch)
    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(profiles, "create_profile_api", lambda *a, **k: {"unexpected": "create"})
    monkeypatch.setattr(profiles, "delete_profile_api", lambda *a, **k: {"unexpected": "delete"})

    handler = _Handler(path, payload)
    routes.handle_post(handler, SimpleNamespace(path=path))

    assert handler.status == 403
    assert "profile management" in handler.response_json["error"].lower()


def test_frontend_records_profile_lock_from_active_profile_payload():
    boot = open("static/boot.js", encoding="utf-8").read()
    ui = open("static/ui.js", encoding="utf-8").read()

    assert "profilePolicyMode:'normal'" in ui
    assert "S.profileLocked=!!p.profile_locked" in boot
    assert "S.lockedProfile=p.locked_profile||null" in boot
    assert "S.profilePolicyMode=p.profile_policy_mode||(S.profileLocked?'locked':'normal')" in boot


def test_frontend_locked_profile_chip_does_not_open_dropdown():
    panels = open("static/panels.js", encoding="utf-8").read()

    assert "S.profilePolicyMode==='locked'" in panels
    assert "profileChip" in panels
    assert "title" in panels
    assert "return;" in panels


def test_frontend_allowlist_can_activate_profiles_but_not_manage_them():
    panels = open("static/panels.js", encoding="utf-8").read()

    assert "if(S.profilePolicyMode==='locked'){ hide(actBtn); hide(delBtn);" in panels
    assert "if(S.profileLocked||isDefault) hide(delBtn); else show(delBtn);" in panels
    assert "if (S.profileLocked) { showToast('Profile management is disabled on this WebUI instance'); return; }" in panels
