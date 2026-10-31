"""Regression for #8249.

Opening a Desktop/CLI session in WebUI must keep the provider that session was
created on. ``sessions.model`` is a bare id. If import drops the provider, chat
start attaches the profile's active provider and bills the wrong account.
"""
import json
import sqlite3

from api.agent_sessions import (
    _model_provider_from_agent_row,
    read_importable_agent_session_rows,
)


def test_model_provider_prefers_selected_provider_over_billing_identity():
    row = {
        "model_config": json.dumps({"model": "grok-4.7", "provider": "xai-oauth"}),
        "billing_provider": "opencode-go",
    }
    assert _model_provider_from_agent_row(row) == "xai-oauth"


def test_model_provider_falls_back_to_billing_identity():
    assert _model_provider_from_agent_row({"billing_provider": "xai-oauth"}) == "xai-oauth"
    assert _model_provider_from_agent_row({"model_config": json.dumps({"provider": "default"})}) is None
    assert _model_provider_from_agent_row({}) is None


def _desktop_db(path, *, model_config, billing_provider):
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            model TEXT,
            message_count INTEGER,
            started_at REAL,
            source TEXT,
            session_source TEXT,
            model_config TEXT,
            billing_provider TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO sessions (
            id, title, model, message_count, started_at, source, session_source,
            model_config, billing_provider
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "desktop-1",
            "Desktop session",
            "grok-4.7",
            1,
            1000.0,
            "desktop",
            "desktop",
            model_config,
            billing_provider,
        ),
    )
    conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)"
    )
    conn.execute(
        "INSERT INTO messages (session_id, role, timestamp) VALUES (?, ?, ?)",
        ("desktop-1", "user", 1001.0),
    )
    conn.commit()
    conn.close()


def test_importable_desktop_row_publishes_selected_provider_not_raw_config(tmp_path):
    db = tmp_path / "state.db"
    _desktop_db(
        db,
        model_config=json.dumps({"model": "grok-4.7", "provider": "xai-oauth"}),
        billing_provider="xai-oauth",
    )
    rows = read_importable_agent_session_rows(db, exclude_sources=None)
    match = [row for row in rows if row["id"] == "desktop-1"]
    assert len(match) == 1
    row = match[0]
    assert row["model"] == "grok-4.7"
    assert row["model_provider"] == "xai-oauth"
    assert "model_config" not in row
    assert "billing_provider" not in row


def test_importable_row_without_provider_does_not_invent_one(tmp_path):
    db = tmp_path / "state.db"
    _desktop_db(db, model_config=None, billing_provider=None)
    rows = read_importable_agent_session_rows(db, exclude_sources=None)
    row = next(item for item in rows if item["id"] == "desktop-1")
    assert row["model"] == "grok-4.7"
    assert "model_provider" not in row


def test_import_cli_session_preserves_model_provider():
    from api.models import SESSION_DIR, Session, import_cli_session

    sid = "import_provider_8249"
    path = SESSION_DIR / f"{sid}.json"
    path.unlink(missing_ok=True)
    try:
        session = import_cli_session(
            sid,
            "Imported",
            [{"role": "user", "content": "hi", "timestamp": 1.0}],
            model="grok-4.7",
            model_provider="xai-oauth",
            created_at=1.0,
            updated_at=2.0,
        )
        assert session.model_provider == "xai-oauth"
        loaded = Session.load(sid)
        assert loaded.model == "grok-4.7"
        assert loaded.model_provider == "xai-oauth"
    finally:
        path.unlink(missing_ok=True)


def test_reimport_backfill_skips_a_changed_model():
    from api.agent_sessions import model_provider_backfill

    assert model_provider_backfill("grok-4.7", None, "grok-4.7", "xai-oauth") == "xai-oauth"
    assert model_provider_backfill("deepseek-v4.1-flash", None, "grok-4.7", "xai-oauth") is None
    assert model_provider_backfill("grok-4.7", "opencode-go", "grok-4.7", "xai-oauth") is None
    assert model_provider_backfill("unknown", None, "grok-4.7", "xai-oauth") == "xai-oauth"
    assert model_provider_backfill("grok-4.7", None, "grok-4.7", "custom") == "custom"
    assert model_provider_backfill("grok-4.7", None, "grok-4.7", "auto") is None


def test_claim_row_reads_provider_without_the_recent_list(tmp_path):
    from api.agent_sessions import read_state_db_claim_row

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            model TEXT,
            cwd TEXT,
            started_at REAL,
            ended_at REAL,
            model_config TEXT,
            billing_provider TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "old-desktop",
            "desktop",
            "Older chat",
            "grok-4.7",
            "/tmp",
            10.0,
            11.0,
            json.dumps({"provider": "xai-oauth"}),
            "xai-oauth",
        ),
    )
    conn.commit()
    conn.close()

    row = read_state_db_claim_row(db, "old-desktop")
    assert row["model"] == "grok-4.7"
    assert row["model_provider"] == "xai-oauth"
    assert "model_config" not in row
    assert "billing_provider" not in row


def test_bare_billing_buckets_are_not_imported_as_providers(tmp_path):
    from api.agent_sessions import _model_provider_from_agent_row, read_state_db_claim_row

    assert _model_provider_from_agent_row({"billing_provider": "custom"}) is None
    assert _model_provider_from_agent_row({"billing_provider": "auto"}) is None
    assert _model_provider_from_agent_row({"model_config": json.dumps({"provider": "custom"})}) == "custom"
    assert _model_provider_from_agent_row({"billing_provider": "xai-oauth"}) == "xai-oauth"
    assert _model_provider_from_agent_row({
        "model_config": json.dumps({"provider": "custom:my-relay"}),
        "billing_provider": "custom",
    }) == "custom:my-relay"
    assert _model_provider_from_agent_row({
        "model_config": json.dumps({
            "provider": "xai-oauth",
            "gateway_runtime": {"provider": "opencode-go"},
        }),
        "billing_provider": "custom",
    }) == "opencode-go"
    assert _model_provider_from_agent_row({
        "model_config": json.dumps({"gateway_runtime": {"provider": "custom", "base_url": "http://relay"}}),
    }) is None
    assert _model_provider_from_agent_row({
        "model_config": json.dumps({"gateway_runtime": {"provider": "custom:my-relay"}}),
    }) == "custom:my-relay"

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            model TEXT,
            billing_provider TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        ("bucket", "cli", "Bucket", "grok-4.7", "custom"),
    )
    conn.commit()
    conn.close()
    row = read_state_db_claim_row(db, "bucket")
    assert row["model"] == "grok-4.7"
    assert "model_provider" not in row


def test_claim_row_survives_a_db_without_provider_columns(tmp_path):
    from api.agent_sessions import read_state_db_claim_row

    db = tmp_path / "state.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT,
            title TEXT,
            model TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?)",
        ("plain", "cli", "Plain", "grok-4.7"),
    )
    conn.commit()
    conn.close()

    row = read_state_db_claim_row(db, "plain")
    assert row["source"] == "cli"
    assert row["model"] == "grok-4.7"
    assert "model_provider" not in row


# ── Routability gate ─────────────────────────────────────────────────────────
# Import may keep a Desktop/CLI provider only when the owning profile can route
# it. A provider the profile no longer has must stay unset, so the first send
# uses the profile provider exactly as before #8249 instead of failing.

import io
import sys
import os
import shutil
import types
from pathlib import Path

import pytest

_GATE_PROFILE = "p8249gate"
_GATE_CONFIG = """\
model:
  provider: custom:available
  default: grok-4.7
custom_providers:
  - name: available
    base_url: http://127.0.0.1:18888/v1
    api_key: sk-available
  - name: relay-b
    base_url: http://127.0.0.1:18999/v1
    api_key: sk-relay-b
  - name: no-endpoint-relay
    api_key: sk-no-endpoint
  - name: unset-key-relay
    base_url: http://127.0.0.1:18666/v1
    key_env: HERMES_8249_UNSET_KEY
  - name: keyless-relay
    base_url: http://127.0.0.1:18555/v1
  - name: env-key-relay
    base_url: http://127.0.0.1:18444/v1
    key_env: HERMES_8249_RELAY_KEY
providers:
  deepseek: {}
  endpoint-builtin:
    base_url: http://127.0.0.1:18777/v1
  unresolved-builtin:
    api_key: ${HERMES_8249_UNSET_KEY}
"""
# Env names the stub resolver treats as credentials (see ``stub_resolver``).
_GATE_KEY_ENVS = (
    "OPENROUTER_API_KEY",
    "XAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "HERMES_8249_UNSET_KEY",
    "HERMES_8249_RELAY_KEY",
)


@pytest.fixture
def gate_profile(monkeypatch):
    """A named profile whose real home holds only ``_GATE_CONFIG``."""
    import api.config as api_config
    import api.profiles as profiles

    home = Path(profiles.get_hermes_home_for_profile(_GATE_PROFILE))
    assert home.name == _GATE_PROFILE, home
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    (home / "config.yaml").write_text(_GATE_CONFIG, encoding="utf-8")
    # The only credential is in this profile's own .env, not the process env.
    (home / ".env").write_text(
        "OPENROUTER_API_KEY=sk-or-gate-test\nHERMES_8249_RELAY_KEY=sk-env-relay\n", encoding="utf-8"
    )
    for name in _GATE_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    # The test environment pins one shared config file; production resolves the
    # config from the active profile's home, which is what the gate must read.
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    api_config.reload_config()
    yield home
    shutil.rmtree(home, ignore_errors=True)
    api_config.reload_config()


@pytest.fixture
def stub_resolver(monkeypatch):
    """Stand-in for the Agent resolver.

    Like the real one, it finds a credential only when ``<PID>_API_KEY`` is in
    the environment it runs in, so a key that lives only in the profile .env is
    found only inside that profile's scope.
    """
    calls = []

    def resolve_runtime_provider(*, requested=None, **_kwargs):
        calls.append(requested)
        key = os.environ.get(f"{str(requested).upper().replace('-', '_')}_API_KEY")
        if key:
            return {"provider": requested, "api_key": key, "base_url": "https://example.invalid/v1"}
        raise RuntimeError(f"No usable credentials found for provider {requested!r}")

    module = types.ModuleType("hermes_cli.runtime_provider")
    module.resolve_runtime_provider = resolve_runtime_provider
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", module)
    return calls


def test_gate_keeps_only_providers_the_owning_profile_can_route(gate_profile, stub_resolver):
    from api.routes import _routable_imported_model_provider as gate

    # Named custom relays: kept only when this profile's config owns them.
    assert gate("custom:relay-b", _GATE_PROFILE) == "custom:relay-b"
    assert gate("custom:removed-relay", _GATE_PROFILE) is None
    # A record is not a route. The send path refuses a relay with a key but no
    # endpoint, or with a declared key_env that does not resolve, while an
    # unpinned session reaches the profile's working relay.
    assert gate("custom:no-endpoint-relay", _GATE_PROFILE) is None
    assert gate("custom:unset-key-relay", _GATE_PROFILE) is None
    # Keyless endpoints and a key_env that resolves from the profile .env work.
    assert gate("custom:keyless-relay", _GATE_PROFILE) == "custom:keyless-relay"
    assert gate("custom:env-key-relay", _GATE_PROFILE) == "custom:env-key-relay"
    # The profile's own provider is routed the same way an unpinned session is.
    assert gate("custom:available", _GATE_PROFILE) == "custom:available"
    # A bare `custom` names the profile's own endpoint. This profile is on a
    # named relay, so the Agent would resolve it to an ambient fallback.
    assert gate("custom", _GATE_PROFILE) is None
    # Built-ins: only a credential from the Agent resolver, found in the
    # profile's .env, counts.
    assert gate("openrouter", _GATE_PROFILE) == "openrouter"
    assert gate("xai-oauth", _GATE_PROFILE) is None
    # A providers: entry is not a credential: empty, endpoint-only, or an
    # unresolved ${VAR} key.
    assert gate("deepseek", _GATE_PROFILE) is None
    assert gate("endpoint-builtin", _GATE_PROFILE) is None
    assert gate("unresolved-builtin", _GATE_PROFILE) is None
    assert gate(None, _GATE_PROFILE) is None
    assert gate("default", _GATE_PROFILE) is None
    # The unowned relay and the profile's own provider never reach the
    # resolver; an owned relay and every built-in do.
    assert stub_resolver == [
        "custom:relay-b",
        "custom:no-endpoint-relay",
        "custom:unset-key-relay",
        "custom:keyless-relay",
        "custom:env-key-relay",
        "openrouter",
        "xai-oauth",
        "deepseek",
        "endpoint-builtin",
        "unresolved-builtin",
    ]


def test_gate_binds_the_owner_scope_when_it_is_already_the_request_profile(gate_profile, stub_resolver):
    from api.profiles import clear_request_profile, set_request_profile
    from api.routes import _routable_imported_model_provider as gate

    # A normal named-profile request: TLS already names the owner, but that
    # does not apply the owner's .env, where the only key lives.
    set_request_profile(_GATE_PROFILE)
    try:
        assert gate("openrouter", _GATE_PROFILE) == "openrouter"
        # A row without a recorded profile is owned by the request profile.
        assert gate("openrouter", None) == "openrouter"
        assert gate("xai-oauth", _GATE_PROFILE) is None
    finally:
        clear_request_profile()
    assert "OPENROUTER_API_KEY" not in os.environ


def test_gate_fails_closed_when_the_resolver_is_unavailable(gate_profile, monkeypatch):
    from api.routes import _routable_imported_model_provider as gate

    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", None)
    assert gate("openrouter", _GATE_PROFILE) is None
    assert gate("custom:relay-b", _GATE_PROFILE) is None
    # Answers that never needed the Agent are unchanged.
    assert gate("custom:available", _GATE_PROFILE) == "custom:available"
    assert gate("custom:removed-relay", _GATE_PROFILE) is None


def _installed_agent_resolver():
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider
    except Exception:
        return None
    return resolve_runtime_provider


@pytest.mark.skipif(_installed_agent_resolver() is None, reason="installed hermes-agent resolver not importable")
def test_gate_uses_the_installed_agent_resolver(gate_profile, monkeypatch):
    from api.routes import _routable_imported_model_provider as gate

    from api.profiles import clear_request_profile, set_request_profile

    assert gate("openrouter", _GATE_PROFILE) == "openrouter"
    # No xAI login, and `deepseek: {}` in providers: is not a DeepSeek key.
    assert gate("xai-oauth", _GATE_PROFILE) is None
    assert gate("deepseek", _GATE_PROFILE) is None
    assert gate("custom:removed-relay", _GATE_PROFILE) is None
    assert gate("custom:relay-b", _GATE_PROFILE) == "custom:relay-b"
    assert gate("custom:keyless-relay", _GATE_PROFILE) == "custom:keyless-relay"
    assert gate("custom:env-key-relay", _GATE_PROFILE) == "custom:env-key-relay"
    assert gate("custom:no-endpoint-relay", _GATE_PROFILE) is None
    assert gate("custom:unset-key-relay", _GATE_PROFILE) is None
    # Same answer when the owner is already the request profile.
    set_request_profile(_GATE_PROFILE)
    try:
        assert gate("openrouter", _GATE_PROFILE) == "openrouter"
        assert gate("deepseek", _GATE_PROFILE) is None
    finally:
        clear_request_profile()


class _ImportHandler:
    def __init__(self):
        self.status = None
        self.headers = {}
        self.wfile = io.BytesIO()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers[key] = value

    def end_headers(self):
        pass


def _desktop_meta(provider):
    return {
        "session_id": "unused",
        "title": "Desktop session",
        "model": "grok-4.7",
        "model_provider": provider,
        "profile": _GATE_PROFILE,
        "source_tag": "desktop",
        "raw_source": "desktop",
        "session_source": "desktop",
        "created_at": 10.0,
        "updated_at": 11.0,
    }


def _stub_cli_store(monkeypatch, routes, provider):
    msgs = [{"role": "user", "content": "hi", "timestamp": 10.0}]
    monkeypatch.setattr(routes, "_resolve_cli_import_metadata", lambda *a, **k: _desktop_meta(provider))
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda *a, **k: list(msgs))
    monkeypatch.setattr(routes, "_session_visible_to_active_profile", lambda *a, **k: True)
    captured = {}
    monkeypatch.setattr(routes, "j", lambda h, payload, status=200, *a, **k: captured.update(payload) or True)
    monkeypatch.setattr(routes, "bad", lambda h, msg, status=400: captured.update(error=(msg, status)) or True)
    return captured


@pytest.mark.parametrize(
    ("desktop_provider", "expected"),
    [
        ("custom:relay-b", "custom:relay-b"),
        ("custom:removed-relay", None),
        ("xai-oauth", None),
    ],
)
def test_first_import_pins_only_a_routable_provider(gate_profile, stub_resolver, monkeypatch, desktop_provider, expected):
    import api.routes as routes
    from api.models import SESSION_DIR, Session

    sid = "import_gate_8249"
    (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)
    captured = _stub_cli_store(monkeypatch, routes, desktop_provider)
    try:
        routes._handle_session_import_cli(_ImportHandler(), {"session_id": sid})
        assert "error" not in captured, captured
        loaded = Session.load(sid)
        assert loaded.model == "grok-4.7"
        assert loaded.model_provider == expected
    finally:
        (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("desktop_provider", "expected"),
    [
        ("custom:relay-b", "custom:relay-b"),
        ("custom:removed-relay", None),
        ("xai-oauth", None),
    ],
)
def test_reimport_backfills_only_a_routable_provider(gate_profile, stub_resolver, monkeypatch, desktop_provider, expected):
    import api.routes as routes
    from api.models import SESSION_DIR, Session, import_cli_session

    sid = "reimport_gate_8249"
    (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)
    # A sidecar imported before #8249: same model, no provider.
    import_cli_session(
        sid,
        "Desktop session",
        [{"role": "user", "content": "hi", "timestamp": 10.0}],
        model="grok-4.7",
        profile=_GATE_PROFILE,
        created_at=10.0,
        updated_at=11.0,
    )
    captured = _stub_cli_store(monkeypatch, routes, desktop_provider)
    try:
        routes._handle_session_import_cli(_ImportHandler(), {"session_id": sid})
        assert "error" not in captured, captured
        assert Session.load(sid).model_provider == expected
    finally:
        (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)


@pytest.mark.parametrize(
    ("desktop_provider", "expected"),
    [
        ("custom:relay-b", "custom:relay-b"),
        ("custom:removed-relay", None),
        ("xai-oauth", None),
    ],
)
def test_saved_link_claim_pins_only_a_routable_provider(gate_profile, stub_resolver, monkeypatch, desktop_provider, expected):
    import api.routes as routes

    sid = "claim_gate_8249"
    monkeypatch.setattr(routes, "get_cli_session_messages", lambda *a, **k: [{"role": "user", "content": "hi"}])
    monkeypatch.setattr(routes, "_session_index_marks_was_webui", lambda *a, **k: False)
    session, reason = routes._claim_or_synthesize_cli_session(sid, _desktop_meta(desktop_provider))
    assert reason == "materialized"
    assert session.model == "grok-4.7"
    assert session.model_provider == expected


@pytest.mark.parametrize("desktop_provider", ["deepseek", "endpoint-builtin", "unresolved-builtin"])
def test_providers_entry_without_a_key_falls_back_to_the_profile_route(
    gate_profile, stub_resolver, monkeypatch, desktop_provider
):
    import api.config as api_config
    import api.routes as routes
    from api.models import SESSION_DIR, Session

    sid = "import_gate_providers_8249"
    (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)
    captured = _stub_cli_store(monkeypatch, routes, desktop_provider)
    try:
        routes._handle_session_import_cli(_ImportHandler(), {"session_id": sid})
        assert "error" not in captured, captured
        loaded = Session.load(sid)
        assert loaded.model_provider is None

        # The first send resolves the session's model the way the chat
        # handler does, against the owning profile's config.
        from api.profiles import profile_scope_for_detached_worker

        with profile_scope_for_detached_worker(_GATE_PROFILE, "test first send"):
            api_config.reload_config()
            _model, provider, _base_url = api_config.resolve_model_provider(
                api_config.model_with_provider_context(loaded.model, loaded.model_provider)
            )
            assert provider == "custom:available"
            assert api_config.resolve_custom_provider_connection(provider) == (
                "sk-available",
                "http://127.0.0.1:18888/v1",
            )
    finally:
        (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)


def test_effective_runtime_route_is_never_replaced_by_an_older_provider():
    """The Agent reads ``gateway_runtime`` first and stops there."""
    stale = {"provider": "custom:relay-a", "base_url": "http://relay-a/v1"}

    def row(runtime, billing="custom:relay-a"):
        return {
            "model_config": json.dumps({**stale, "gateway_runtime": runtime}),
            "billing_provider": billing,
        }

    # A bare `custom` runtime names an endpoint, not a provider. The older
    # selected and billing providers were replaced by it, so nothing is pinned.
    assert _model_provider_from_agent_row(
        row({"provider": "custom", "base_url": "http://relay-b/v1"})
    ) is None
    assert _model_provider_from_agent_row(row({"provider": "auto"})) is None
    assert _model_provider_from_agent_row(row({"provider": "default"})) is None
    # A named runtime still wins over the older sources.
    assert _model_provider_from_agent_row(
        row({"provider": "custom:relay-b", "base_url": "http://relay-b/v1"})
    ) == "custom:relay-b"
    # Without a runtime route the older sources still apply.
    assert _model_provider_from_agent_row(row({})) == "custom:relay-a"
    assert _model_provider_from_agent_row(row({"provider": None, "base_url": None})) == "custom:relay-a"


def test_effective_runtime_route_publishes_no_stale_provider_in_the_list_projection(tmp_path):
    db = tmp_path / "state.db"
    _desktop_db(
        db,
        model_config=json.dumps({
            "provider": "custom:relay-a",
            "gateway_runtime": {"provider": "custom", "base_url": "http://relay-b/v1"},
        }),
        billing_provider="custom:relay-a",
    )
    rows = read_importable_agent_session_rows(db, limit=10)
    assert rows, "the Desktop row should be importable"
    assert all("model_provider" not in r for r in rows), rows


@pytest.mark.parametrize("desktop_provider", ["custom:no-endpoint-relay", "custom:unset-key-relay"])
def test_unusable_named_relay_is_unpinned_and_the_first_send_reaches_the_working_route(
    gate_profile, stub_resolver, monkeypatch, desktop_provider
):
    import api.config as api_config
    import api.routes as routes
    from api.models import SESSION_DIR, Session
    from api.profiles import profile_scope_for_detached_worker

    # Control: pinning this relay is exactly what the send path refuses.
    with profile_scope_for_detached_worker(_GATE_PROFILE, "test pinned send"):
        api_config.reload_config()
        with pytest.raises(api_config.CustomProviderRouteError) as refused:
            routes._resolve_agent_connection_bundle(desktop_provider, None, None, None)
    assert refused.value.reason in {
        api_config.CUSTOM_ROUTE_NO_ENDPOINT,
        api_config.CUSTOM_ROUTE_NO_CREDENTIAL,
    }

    sid = "import_gate_unusable_relay_8249"
    (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)
    captured = _stub_cli_store(monkeypatch, routes, desktop_provider)
    try:
        routes._handle_session_import_cli(_ImportHandler(), {"session_id": sid})
        assert "error" not in captured, captured
        loaded = Session.load(sid)
        assert loaded.model_provider is None
        with profile_scope_for_detached_worker(_GATE_PROFILE, "test first send"):
            api_config.reload_config()
            _model, provider, _base_url = api_config.resolve_model_provider(
                api_config.model_with_provider_context(loaded.model, loaded.model_provider)
            )
            assert provider == "custom:available"
            bundle = routes._resolve_agent_connection_bundle(provider, None, None, None)
            assert (bundle["api_key"], bundle["base_url"]) == ("sk-available", "http://127.0.0.1:18888/v1")
    finally:
        (SESSION_DIR / f"{sid}.json").unlink(missing_ok=True)
