"""Custom `.env` key management — the WebUI-only credential path (#7815).

A WebUI-only user (no shell, no dashboard) previously had no in-app way to
store a skill's credential: the options were pasting the secret into the chat
(where it lands in session history and goes to the model provider) or asking
an operator to hand-edit ``.env``. The dashboard's Keys page solves it with
``GET/PUT/DELETE /api/env``; this ports that contract to the WebUI.

CI for hermes-webui does not install hermes-agent, so these tests inject a
tiny fake ``hermes_cli.config`` that owns a real temp file — enough to prove
the handler contract (redaction, name validation, reserved-family refusal,
delete idempotence, plaintext never echoed) against the agent's writer API
without the external package.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest


# ── the fake agent side ──────────────────────────────────────────────────────


class _FakeEnvStore:
    """Stands in for ``~/.hermes/.env`` plus the writer's denylist check."""

    def __init__(self):
        self.data: dict[str, str] = {}
        self.writes: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.denylist = frozenset({"PATH", "LD_PRELOAD", "HERMES_YOLO_MODE"})

    def load_env(self) -> dict[str, str]:
        return dict(self.data)

    def save_env_value(self, key: str, value: str) -> None:
        if key in self.denylist:
            raise ValueError(
                f"Environment variable {key!r} is on the writer denylist. "
                "Use a different variable name."
            )
        self.writes.append((key, value))
        self.data[key] = value

    def remove_env_value(self, key: str) -> bool:
        self.deletes.append(key)
        return self.data.pop(key, None) is not None


def _parsed(path="/api/env/keys", query=""):
    return SimpleNamespace(path=path, query=query)


class _RecordingHandler:
    """Captures the ``j()`` / ``bad()`` response instead of writing a socket."""

    def __init__(self):
        self.status: int | None = None
        self.payload = None


def _install_agent(monkeypatch, store: _FakeEnvStore):
    fake_cli = types.ModuleType("hermes_cli")
    fake_config = types.ModuleType("hermes_cli.config")
    fake_config.load_env = store.load_env  # type: ignore[attr-defined]
    fake_config.save_env_value = store.save_env_value  # type: ignore[attr-defined]
    fake_config.remove_env_value = store.remove_env_value  # type: ignore[attr-defined]
    fake_cli.config = fake_config  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", fake_config)

    # A REAL profile-scope binding of the installed agent's shape (raises on
    # an unknown name, no silent no-op fallback) — see #7870's authorization
    # fix: a request must act on the WebUI-authenticated profile's home, and a
    # binding that cannot be built is an error, not a fallback to the launch
    # profile's .env.
    fake_scope_mod = types.ModuleType("hermes_cli.web_server_profiles")

    class _Scope:
        def __init__(self, profile):
            self._profile = profile

        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    def _profile_scope(profile):
        if not profile or not isinstance(profile, str):
            raise ValueError(f"unusable profile {profile!r}")
        return _Scope(profile)

    fake_scope_mod._profile_scope = _profile_scope  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_profiles", fake_scope_mod)

    # `api.env_keys._profile_scope` also imports `hermes_constants` for the
    # context-local home override (`set/reset_hermes_home_override`). CI does
    # not install hermes-agent, so the real module is absent there; a call to
    # the production `_profile_scope` would fail closed (EnvKeyProfileError ->
    # 500/503) purely on that import. Stub the module with a contextvar-backed
    # override that mirrors the real API, so the handlers exercise the profile
    # scoping without needing the agent installed.
    import contextvars

    _home_override_var: "contextvars.ContextVar" = contextvars.ContextVar(
        "hermes_home_override", default=None
    )

    def _set_override(path):
        return _home_override_var.set(None if path is None else str(path))

    fake_hc = types.ModuleType("hermes_constants")
    fake_hc.set_hermes_home_override = _set_override  # type: ignore[attr-defined]
    fake_hc.reset_hermes_home_override = (  # type: ignore[attr-defined]
        _home_override_var.reset
    )
    fake_hc.get_hermes_home_override = (  # type: ignore[attr-defined]
        _home_override_var.get
    )
    monkeypatch.setitem(sys.modules, "hermes_constants", fake_hc)

    # WebUI's authorization source for the request's profile.
    import api.profiles as profiles

    monkeypatch.setattr(
        profiles, "get_active_profile_name", lambda: "default", raising=False
    )


@pytest.fixture
def env_store(monkeypatch):
    """Reload ``api.env_keys`` with the fake agent writer injected."""
    import importlib

    import api.env_keys as env_keys

    store = _FakeEnvStore()
    _install_agent(monkeypatch, store)
    return importlib.reload(env_keys), store


# ── response capture ─────────────────────────────────────────────────────────


class _Capture:
    """Context manager that records what ``api.helpers.j`` / ``.bad`` send.

    Reloads the module under test so its ``from api.helpers import bad, j``
    module-level names bind to the patched functions (the module does
    ``from api.helpers import ...``, so patching ``helpers`` after import
    would not be seen).
    """

    def __init__(self, handler: _RecordingHandler):
        self.handler = handler
        self.status: int | None = None
        self.payload = None
        self._api_env_keys = None

    def __enter__(self):
        import api.helpers as helpers

        outer = self

        def _fake_j(h, payload, status=200, extra_headers=None, *, pretty=True):
            outer.status = status
            outer.payload = payload
            return True

        def _fake_bad(h, msg, status=400):
            outer.status = status
            outer.payload = {"error": msg}
            return True

        self._orig_j, self._orig_bad = helpers.j, helpers.bad
        helpers.j, helpers.bad = _fake_j, _fake_bad

        import importlib

        import api.env_keys

        self._api_env_keys = importlib.reload(api.env_keys)
        return self

    def __exit__(self, *exc):
        import api.helpers as helpers

        helpers.j, helpers.bad = self._orig_j, self._orig_bad
        return False


# ── GET: redacted listing ────────────────────────────────────────────────────


def test_list_returns_redacted_previews_not_plaintext(env_store):
    module, store = env_store
    store.data["WP_APP_PASSWORD"] = "super-secret-value"

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        assert module.handle_env_keys_get(handler, _parsed()) is True

    assert captured.status == 200
    keys = {row["name"]: row for row in captured.payload["keys"]}
    row = keys["WP_APP_PASSWORD"]
    assert row["is_set"] is True
    assert row["redacted_value"] != "super-secret-value"
    assert "super-secret-value" not in str(captured.payload)
    # #7870 review: the preview reveals NOTHING — not even the first and last
    # two characters, which is a real leak for the many credentials built as
    # <shared-family-prefix><random><shared-suffix>. The mask keeps the LENGTH
    # only, which is what a user needs to tell two keys apart.
    assert row["redacted_value"] == "*" * len("super-secret-value")
    assert not any(ch in row["redacted_value"] for ch in "super-value")


def test_list_marks_reserved_families_as_managed_elsewhere(env_store):
    module, store = env_store
    store.data["HERMES_GATEWAY_TOKEN"] = "x" * 12
    store.data["MY_SKILL_TOKEN"] = "y" * 12

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_keys_get(handler, _parsed())

    rows = {row["name"]: row for row in captured.payload["keys"]}
    assert rows["HERMES_GATEWAY_TOKEN"]["managed_elsewhere"] is True
    assert rows["MY_SKILL_TOKEN"]["managed_elsewhere"] is False


def test_list_survives_a_long_value(env_store):
    module, store = env_store
    store.data["SHORT"] = "abc"

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_keys_get(handler, _parsed())

    row = {r["name"]: r for r in captured.payload["keys"]}["SHORT"]
    assert row["redacted_value"] == "***"  # short values are fully masked


# ── PUT: add / replace ───────────────────────────────────────────────────────


def test_put_writes_through_the_agent_writer(env_store):
    module, store = env_store

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_keys_put(
            handler, _parsed(), {"name": "WP_APP_PASSWORD", "value": "topsecret"}
        )

    assert captured.status == 200
    assert store.writes == [("WP_APP_PASSWORD", "topsecret")]
    assert store.data["WP_APP_PASSWORD"] == "topsecret"
    # The response echoes the redacted shape a GET would, never the plaintext.
    assert "topsecret" not in str(captured.payload)
    assert captured.payload["key"]["name"] == "WP_APP_PASSWORD"


def test_put_rejects_an_empty_value(env_store):
    module, store = env_store

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_keys_put(handler, _parsed(), {"name": "A_KEY", "value": ""})

    assert captured.status == 400
    assert store.writes == []


@pytest.mark.parametrize("bad_name", ["1BAD", "has space", "dash-ed", ""])
def test_put_rejects_a_malformed_name(env_store, bad_name):
    module, store = env_store

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_keys_put(handler, _parsed(), {"name": bad_name, "value": "v"})

    assert captured.status == 400
    assert store.writes == []


def test_put_refuses_a_reserved_family_name(env_store):
    module, store = env_store

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_keys_put(
            handler, _parsed(), {"name": "HERMES_YOLO_MODE", "value": "1"}
        )

    assert captured.status == 409
    assert store.writes == []


def test_put_surfaces_the_writer_denylist_as_a_400(env_store):
    module, store = env_store

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_keys_put(handler, _parsed(), {"name": "LD_PRELOAD", "value": "/x"})

    assert captured.status == 400
    assert "denylist" in captured.payload["error"].lower()
    assert "/x" not in captured.payload["error"]


# ── DELETE: remove ───────────────────────────────────────────────────────────


def test_delete_removes_the_key_through_the_agent_writer(env_store):
    module, store = env_store
    store.data["OLD_KEY"] = "gone soon"

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_key_delete(handler, "OLD_KEY", _parsed("/api/env/keys/OLD_KEY"))

    assert captured.status == 200
    assert store.deletes == ["OLD_KEY"]
    assert "OLD_KEY" not in store.data


def test_delete_of_an_unknown_key_is_a_no_op_success(env_store):
    module, store = env_store

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_key_delete(
            handler, "NEVER_EXISTED", _parsed("/api/env/keys/NEVER_EXISTED")
        )

    assert captured.status == 200  # the user's intent already holds
    assert store.deletes == ["NEVER_EXISTED"]


def test_delete_refuses_a_reserved_family_name(env_store):
    module, store = env_store
    store.data["HERMES_CUSTOM_X_API_KEY"] = "v" * 12

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_key_delete(
            handler, "HERMES_CUSTOM_X_API_KEY", _parsed("/api/env/keys/HERMES_CUSTOM_X_API_KEY")
        )

    assert captured.status == 409
    assert store.deletes == []


def test_delete_rejects_a_malformed_name(env_store):
    module, store = env_store

    handler = _RecordingHandler()
    with _Capture(handler) as captured:
        module.handle_env_key_delete(handler, "1BAD", _parsed("/api/env/keys/1BAD"))

    assert captured.status == 400
    assert store.deletes == []
