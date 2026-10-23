"""#8071 — profile-scoped messaging channel setup (Telegram-first).

Contract under test (maintainer-approved scope for #8071):

* operator-only, scoped to the ACTIVE profile;
* configured/enabled status is reported, token values are NEVER read back;
* the UNSAVED candidate token is validated BEFORE it is persisted;
* a bot token already owned by another profile on the same host is refused
  (only one poller per token can work);
* the write lands in the active profile's own ``.env`` and nothing else.

These are deliberately narrow tests around the security boundaries. The
"topology-safe apply to the proved target Gateway" leg is covered by the
apply-module tests, not here.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.sent_headers: list[tuple[str, str]] = []
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        self.sent_headers.append((key, value))

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data if isinstance(data, (bytes, bytearray)) else data.encode("utf-8"))

    def get_json(self):
        return json.loads(self.body.decode("utf-8"))


TOKEN_A = "111111:AAHfixturetokenvalue0000000000000000"
TOKEN_B = "222222:BBHfixturetokenvalue1111111111111111"


def _seed_profile(tmp_path, name: str, token: str | None = None, allowed: str | None = None):
    """Create a fake profile home with an optional .env."""
    home = tmp_path / "profiles" / name
    home.mkdir(parents=True, exist_ok=True)
    if token is not None or allowed is not None:
        lines = ["# fixture env"]
        if token is not None:
            lines.append(f"TELEGRAM_BOT_TOKEN={token}")
        if allowed is not None:
            lines.append(f"TELEGRAM_ALLOWED_USERS={allowed}")
        (home / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return home


def _wire(monkeypatch, tmp_path, active: str, homes: dict[str, object]):
    """Point the messaging module at fixture profile homes."""
    from api import messaging, profiles

    def _home_for(name: str):
        if name not in homes:
            raise AssertionError(f"unexpected profile lookup: {name!r}")
        return homes[name]

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: active)
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: _home_for(active))
    monkeypatch.setattr(profiles, "get_hermes_home_for_profile", _home_for)
    monkeypatch.setattr(profiles, "list_profiles_api", lambda: [{"name": n} for n in homes])
    return messaging


def _call_get(monkeypatch, path: str):
    from api import routes

    monkeypatch.setattr(routes, "read_body", lambda handler: {})
    handler = _FakeHandler()
    routes.handle_get(handler, urlparse(path))
    return handler, handler.get_json()


def _call_put(monkeypatch, path: str, body: dict):
    from api import routes

    monkeypatch.setattr(routes, "_check_csrf", lambda handler: True)
    monkeypatch.setattr(routes, "read_body", lambda handler: body)
    handler = _FakeHandler()
    routes.handle_put(handler, urlparse(path))
    return handler, handler.get_json()


# ── RED: status listing ──────────────────────────────────────────────────────


def test_list_reports_configured_state_without_reading_back_the_token(monkeypatch, tmp_path):
    home = _seed_profile(tmp_path, "work", token=TOKEN_A, allowed="123")
    _wire(monkeypatch, tmp_path, "work", {"work": home})

    handler, payload = _call_get(monkeypatch, "/api/messaging/platforms")

    assert handler.status == 200
    platforms = {p["id"]: p for p in payload["platforms"]}
    assert platforms["telegram"]["configured"] is True
    assert platforms["telegram"]["enabled"] is True
    # Write-mostly: the token itself must never appear anywhere in the payload.
    assert TOKEN_A not in json.dumps(payload)
    assert "token" not in platforms["telegram"]


def test_list_reports_unconfigured_platform_for_a_fresh_profile(monkeypatch, tmp_path):
    home = _seed_profile(tmp_path, "fresh")
    _wire(monkeypatch, tmp_path, "fresh", {"fresh": home})

    handler, payload = _call_get(monkeypatch, "/api/messaging/platforms")

    assert handler.status == 200
    platforms = {p["id"]: p for p in payload["platforms"]}
    assert platforms["telegram"]["configured"] is False
    assert platforms["telegram"]["enabled"] is False


def test_list_is_scoped_to_the_active_profile_only(monkeypatch, tmp_path):
    other = _seed_profile(tmp_path, "other", token=TOKEN_B)
    active = _seed_profile(tmp_path, "work")
    _wire(monkeypatch, tmp_path, "work", {"work": active, "other": other})

    _handler, payload = _call_get(monkeypatch, "/api/messaging/platforms")

    platforms = {p["id"]: p for p in payload["platforms"]}
    assert platforms["telegram"]["configured"] is False  # active profile has no bot
    assert TOKEN_B not in json.dumps(payload)


# ── RED: candidate validation happens BEFORE persisting ──────────────────────


def test_put_refuses_an_invalid_candidate_token_without_persisting(monkeypatch, tmp_path):
    home = _seed_profile(tmp_path, "work")
    messaging = _wire(monkeypatch, tmp_path, "work", {"work": home})
    monkeypatch.setattr(
        messaging, "_telegram_get_me", lambda token: (False, "Telegram rejected the token")
    )

    handler, payload = _call_put(
        monkeypatch, "/api/messaging/platforms/telegram", {"token": TOKEN_A}
    )

    assert handler.status == 400
    assert "rejected" in payload["error"].lower()
    # The unsaved candidate must not have reached the profile .env.
    assert not (home / ".env").exists()


def test_put_rejects_a_token_with_a_newline_before_it_is_validated(monkeypatch, tmp_path):
    """A newline in the candidate is a .env-injection attempt, not a token."""
    home = _seed_profile(tmp_path, "work")
    messaging = _wire(monkeypatch, tmp_path, "work", {"work": home})

    def _explode(token):  # pragma: no cover - must never run
        raise AssertionError("validation must not run on an injectable candidate")

    monkeypatch.setattr(messaging, "_telegram_get_me", _explode)

    handler, _payload = _call_put(
        monkeypatch,
        "/api/messaging/platforms/telegram",
        {"token": f"{TOKEN_A}\nTELEGRAM_ALLOWED_USERS=evil"},
    )

    assert handler.status == 400
    assert not (home / ".env").exists()


# ── RED: duplicate-token ownership across profiles ───────────────────────────


def test_put_refuses_a_token_already_owned_by_another_profile(monkeypatch, tmp_path):
    other = _seed_profile(tmp_path, "other", token=TOKEN_A)
    work = _seed_profile(tmp_path, "work")
    messaging = _wire(monkeypatch, tmp_path, "work", {"work": work, "other": other})
    # Ownership beats validation: a token another profile already polls is
    # refused even though Telegram would happily accept it.
    monkeypatch.setattr(messaging, "_telegram_get_me", lambda token: (True, "fixture-bot"))

    handler, payload = _call_put(
        monkeypatch, "/api/messaging/platforms/telegram", {"token": TOKEN_A}
    )

    assert handler.status == 409
    assert "other" in payload["error"]
    assert not (work / ".env").exists()


def test_put_allows_the_profile_to_re_save_its_own_token(monkeypatch, tmp_path):
    work = _seed_profile(tmp_path, "work", token=TOKEN_A)
    messaging = _wire(monkeypatch, tmp_path, "work", {"work": work})
    monkeypatch.setattr(messaging, "_telegram_get_me", lambda token: (True, "fixture-bot"))

    handler, _payload = _call_put(
        monkeypatch,
        "/api/messaging/platforms/telegram",
        {"token": TOKEN_A, "allowed_users": ["123"], "apply": False},
    )

    assert handler.status == 200


# ── RED: persistence is profile-scoped ───────────────────────────────────────


def test_put_writes_only_the_active_profile_env(monkeypatch, tmp_path):
    other = _seed_profile(tmp_path, "other", token=TOKEN_B)
    work = _seed_profile(tmp_path, "work")
    messaging = _wire(monkeypatch, tmp_path, "work", {"work": work, "other": other})
    monkeypatch.setattr(messaging, "_telegram_get_me", lambda token: (True, "fixture-bot"))
    other_env_before = (other / ".env").read_text(encoding="utf-8")

    handler, payload = _call_put(
        monkeypatch,
        "/api/messaging/platforms/telegram",
        {"token": TOKEN_A, "allowed_users": ["123", "456"], "apply": False},
    )

    assert handler.status == 200
    env_text = (work / ".env").read_text(encoding="utf-8")
    assert f"TELEGRAM_BOT_TOKEN={TOKEN_A}" in env_text
    assert "TELEGRAM_ALLOWED_USERS=123,456" in env_text
    # The neighbouring profile's bot is untouched.
    assert (other / ".env").read_text(encoding="utf-8") == other_env_before
    # No token read-back in the response.
    assert TOKEN_A not in json.dumps(payload)


def test_put_rejects_an_unknown_platform(monkeypatch, tmp_path):
    home = _seed_profile(tmp_path, "work")
    _wire(monkeypatch, tmp_path, "work", {"work": home})

    handler, _payload = _call_put(
        monkeypatch, "/api/messaging/platforms/carrier-pigeon", {"token": TOKEN_A}
    )

    assert handler.status == 404


@pytest.mark.parametrize("body", [{}, {"token": ""}, {"token": "   "}])
def test_put_requires_a_token(monkeypatch, tmp_path, body):
    home = _seed_profile(tmp_path, "work")
    _wire(monkeypatch, tmp_path, "work", {"work": home})

    handler, _payload = _call_put(monkeypatch, "/api/messaging/platforms/telegram", body)

    assert handler.status == 400


# ── Settings surface wiring ──────────────────────────────────────────────────
# The API is unreachable for an end user without the pane, so guard the wiring
# that a later refactor could silently drop.

_ROOT = Path(__file__).resolve().parents[1]


def test_settings_exposes_a_messaging_section_and_pane():
    html = (_ROOT / "static" / "index.html").read_text(encoding="utf-8")
    assert 'data-settings-section="messaging"' in html
    assert 'id="settingsPaneMessaging"' in html
    # The token field must be a password input, and must not be pre-filled with
    # a value attribute from the server response.
    assert 'type="password" id="msgToken_' in html or 'type="password" id="msgToken_' in (
        _ROOT / "static" / "panels.js"
    ).read_text(encoding="utf-8")


def test_messaging_section_is_switchable_and_lazy_loaded():
    panels = (_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    assert "name==='messaging'" in panels  # accepted by switchSettingsSection
    assert "messaging:'Messaging'" in panels  # present in the pane-name map
    assert "if(section==='messaging') loadMessagingPanel();" in panels


def test_messaging_panel_never_prefills_a_token_from_the_server():
    """A blank token field means 'keep the saved token' — never pre-filled."""
    panels = (_ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    start = panels.index("async function loadMessagingPanel()")
    end = panels.index("async function saveMessagingPlatform(btn)")
    loader = panels[start:end]
    # No server field is copied into the token input's value attribute.
    assert "msgToken_" not in loader
    # Saving only transmits a token the operator actually typed.
    assert "if(token) body.token=token;" in panels