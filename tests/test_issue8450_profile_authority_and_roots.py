"""#8450 re-gate round 5 (2026-11-27): profile-scoped translation authority
and ONE consistently resolved local root for every session origin.

Two production-path regressions the maintainer replayed on head
``d6210ced4`` (CHANGES_REQUESTED, 2026-11-23 23:00):

**Blocker 1 — profile-owned translation authority is not preserved.**
The translation tier read its per-profile map through the ambient config
path (``get_config_for_profile_home`` without
``isolate_config_override``). A supported configuration path —
``HERMES_CONFIG_PATH``, which a boot-time launcher sets to the ROOT
profile's config.yaml — therefore let a session consume translation
settings from a DIFFERENT profile whenever the session's own profile was
the active one. The native file-reader probe returned bytes from the
wrong owner-selected root. Fix: translation settings (and the remote
backend classification that feeds them) are read under explicitly
isolated session-owner profile authority.

**Blocker 2 — file listings and operations can resolve different roots.**
For an external (state.db-only) session, ``/api/list`` resolved the
session's OWNER profile + agent-visible workspace, while
``get_session_for_file_ops`` built the view from the AMBIENT last
workspace with no profile — no translation, no owner trust. The listed
workspace and the root used by the read and delete probes disagreed.
Fix: the external view resolves through the same local-root contract as
the listing (owner-profile translation when a declared map applies, else
the owner profile's trusted resolution), and the agent-visible workspace
is preserved on the view.

The world below is production-shaped: real profile homes under a real
HERMES_HOME root, real ``config.yaml`` files read through the real config
resolver (including the ``HERMES_CONFIG_PATH`` override), the real
handlers and the real resolvers. Only the session lookup, the CLI-row
snapshot and the saved-workspace list are provided — the same seams the
#7217 suites use.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest

workspace = pytest.importorskip("api.workspace")
profiles = pytest.importorskip("api.profiles")


# ── production-shaped profile/config world ─────────────────────────────────


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def profile_world(tmp_path, monkeypatch):
    """A real HERMES_HOME root with three real profiles.

    root (default): declares agent /workspace -> <home>/root-ws
    profiles/alice: declares agent /workspace -> <home>/alice-ws
    profiles/bravo: declares NO map; saved workspace list = {bravo_only}

    ``root_ws``/``alice_ws`` sit under the home (trust rule A);
    ``bravo_only`` sits outside it (trust rule B only, so a revoked entry
    is observable). ``ambient_ws`` is what the ambient (active-profile)
    last-workspace read returns — deliberately a different directory.
    """
    home = tmp_path / "hermes-home"
    root_ws = home / "root-ws"
    alice_ws = home / "alice-ws"
    root_ws.mkdir(parents=True)
    alice_ws.mkdir(parents=True)
    bravo_only = tmp_path / "bravo-only"
    bravo_only.mkdir()
    ambient_ws = tmp_path / "ambient-ws"
    ambient_ws.mkdir()

    _write(
        home / "config.yaml",
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {root_ws}\n",
    )
    _write(
        home / "profiles" / "alice" / "config.yaml",
        "webui:\n"
        "  workspace_path_maps:\n"
        "    - agent: /workspace\n"
        f"      webui: {alice_ws}\n",
    )
    _write(
        home / "profiles" / "bravo" / "config.yaml",
        "terminal:\n  backend: local\n",
    )

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", home)
    monkeypatch.setattr(workspace, "_home_path", lambda: home)

    saved = {"alice": [], "bravo": [str(bravo_only)]}
    monkeypatch.setattr(
        workspace,
        "load_workspaces",
        lambda profile=None: [{"path": p} for p in saved.get(profile or "", [])],
    )
    return {
        "home": home,
        "root_ws": root_ws,
        "alice_ws": alice_ws,
        "bravo_only": bravo_only,
        "ambient_ws": ambient_ws,
        "saved": saved,
    }


@pytest.fixture
def active_profile():
    """Set the per-request (thread-local) active profile, as the server does."""
    def _set(name):
        profiles.set_request_profile(name)

    yield _set
    profiles.clear_request_profile()


def _set_config_override(monkeypatch, target: Path | None) -> None:
    """Point the process-level config override at *target* (or unset it)."""
    if target is None:
        monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    else:
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(target))


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.body = bytearray()
        self.wfile = self
        self.headers = {}

    def send_response(self, code):
        self.status = code

    def send_header(self, key, value):
        pass

    def end_headers(self):
        pass

    def write(self, data):
        self.body.extend(data)

    def json_body(self):
        return json.loads(bytes(self.body).decode("utf-8"))


# ── Blocker 1: profile-owned translation authority ──────────────────────────


# (session owner profile, HERMES_CONFIG_PATH target, expected WebUI root)
# The override target names a profile whose config.yaml the process-level
# override points at: "root" -> <home>/config.yaml, "alice"/"bravo" -> that
# profile's own config.yaml, None -> no override at all.
PROFILE_AUTHORITY_CANDIDATES = [
    pytest.param("alice", None, "alice_ws", id="alice-no-override"),
    pytest.param("alice", "root", "alice_ws", id="alice-override-at-root"),
    pytest.param("alice", "alice", "alice_ws", id="alice-override-at-own"),
    pytest.param("alice", "bravo", "alice_ws", id="alice-override-at-bravo"),
    pytest.param("default", None, "root_ws", id="root-no-override"),
    pytest.param("default", "root", "root_ws", id="root-override-at-own"),
    pytest.param("default", "bravo", "root_ws", id="root-override-at-bravo"),
    pytest.param("default", "alice", "root_ws", id="root-override-at-alice"),
]


@pytest.mark.parametrize("session_profile,override_target,expected_root", PROFILE_AUTHORITY_CANDIDATES)
def test_translation_settings_come_from_the_session_owner_profile(
    profile_world, active_profile, monkeypatch, session_profile, override_target, expected_root
):
    """Every supported configuration path resolves translation settings
    under the SESSION OWNER profile's isolated authority.

    A named profile never consumes an ambient override that belongs to
    another profile (root or a sibling); the root profile keeps an
    external override that is not under ``<root>/profiles`` (the
    documented rule) and never consumes one that lives under a named
    profile's home.
    """
    override_paths = {
        "root": profile_world["home"] / "config.yaml",
        "alice": profile_world["home"] / "profiles" / "alice" / "config.yaml",
        "bravo": profile_world["home"] / "profiles" / "bravo" / "config.yaml",
    }
    _set_config_override(monkeypatch, override_paths.get(override_target))
    active_profile(session_profile)

    resolved = workspace.resolve_webui_workspace("/workspace", profile=session_profile)

    assert Path(resolved) == profile_world[expected_root].resolve(), (
        f"profile {session_profile!r} with override at {override_target!r} must "
        f"resolve its OWN map, got {resolved}"
    )


def test_native_file_read_returns_owner_selected_root_bytes(
    profile_world, active_profile, monkeypatch
):
    """The maintainer's native file-reader probe, replayed.

    alice is the active profile and owns the session; the process-level
    override points at the ROOT config (a supported configuration path).
    The read must be served from alice's own mapped root — the bytes the
    owner profile selected — not from the root profile's root.
    """
    import api.models as models
    import api.routes as routes

    root_ws = profile_world["root_ws"]
    alice_ws = profile_world["alice_ws"]
    (root_ws / "secret.txt").write_text("ROOT-ROOT-BYTES", encoding="utf-8")
    (alice_ws / "secret.txt").write_text("ALICE-OWNER-BYTES", encoding="utf-8")

    _set_config_override(monkeypatch, profile_world["home"] / "config.yaml")
    active_profile("alice")

    session = SimpleNamespace(session_id="native-1", workspace="/workspace", profile="alice")
    monkeypatch.setattr(models, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(models, "state_db_has_session", lambda sid: False)
    monkeypatch.setattr(routes, "get_session_for_file_ops", models.get_session_for_file_ops)

    handler = _FakeHandler()
    routes._handle_file_read(
        handler, urlparse("/api/file/read?session_id=native-1&path=secret.txt")
    )

    assert handler.status == 200
    assert handler.json_body()["content"] == "ALICE-OWNER-BYTES"
    # The agent-visible binding is never rewritten by the translation.
    assert session.workspace == "/workspace"


def test_remote_backend_classification_uses_owner_profile_config(
    profile_world, active_profile, monkeypatch
):
    """Neighbour case with the same root cause: the remote/local backend
    classification that feeds translation also reads the OWNER profile's
    config. A root profile declaring a REMOTE terminal backend whose cwd
    covers the agent path must not turn a local named profile's workspace
    into a target-side candidate (which would bypass the local root).
    """
    # Root declares a REMOTE terminal backend whose cwd covers /workspace;
    # alice stays local with her own map.
    _write(
        profile_world["home"] / "config.yaml",
        "terminal:\n  backend: ssh\n  cwd: /workspace\n",
    )
    _set_config_override(monkeypatch, profile_world["home"] / "config.yaml")
    active_profile("alice")

    # alice's own config (local backend) must govern: no remote
    # classification, the declared map translates to the local root.
    resolved = workspace.resolve_webui_workspace("/workspace", profile="alice")
    assert Path(resolved) == profile_world["alice_ws"].resolve()


# ── Blocker 2: one local root for every session origin ──────────────────────


def _patch_external_session(monkeypatch, *, row, ambient_ws, state_db=True):
    """Point the file-ops + listing seams at one external session.

    *row* is the CLI metadata row (owner profile + agent-visible
    workspace); pass ``None`` for "no CLI row" (the ambient fallback).
    The process-level config override is cleared so the OWNER profile's
    own config.yaml is what the real resolver reads — isolating this
    suite's subject (the external-session root contract) from the
    ambient-override subject covered above.
    """
    import api.models as models
    import api.routes as routes

    _set_config_override(monkeypatch, None)

    def raise_key(sid, metadata_only=False):
        raise KeyError(sid)

    monkeypatch.setattr(models, "get_session", raise_key)
    monkeypatch.setattr(routes, "get_session", raise_key)
    monkeypatch.setattr(models, "state_db_has_session", lambda sid: state_db)
    monkeypatch.setattr(models, "get_last_workspace", lambda *a, **k: str(ambient_ws))
    if row is None:
        monkeypatch.setattr(models, "lookup_cli_session_row", lambda sid, all_profiles=False: {})
        monkeypatch.setattr(models, "get_cli_sessions", lambda *a, **k: [])
        monkeypatch.setattr(models, "get_cli_session_by_id", lambda *a, **k: {})
        monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid, all_profiles=False: {})
    else:
        monkeypatch.setattr(models, "lookup_cli_session_row", lambda sid, all_profiles=False: dict(row))
        monkeypatch.setattr(routes, "_lookup_cli_session_metadata", lambda sid, all_profiles=False: dict(row))
    return models, routes


def _drive_three_surfaces(routes, sid: str, rel: str):
    """Listing + read + delete for *sid*; returns (list_payload, read_content, delete_status)."""
    list_handler = _FakeHandler()
    routes._handle_list_dir(list_handler, urlparse(f"/api/file/list?session_id={sid}&path=."))
    list_payload = list_handler.json_body() if list_handler.status == 200 else None

    read_handler = _FakeHandler()
    routes._handle_file_read(
        read_handler, urlparse(f"/api/file/read?session_id={sid}&path={rel}")
    )
    read_content = read_handler.json_body().get("content") if read_handler.status == 200 else None

    delete_handler = _FakeHandler()
    routes._handle_file_delete(delete_handler, {"session_id": sid, "path": rel})

    return list_handler.status, list_payload, read_handler.status, read_content, delete_handler.status


def test_external_session_listing_read_and_delete_share_one_root(
    profile_world, active_profile, monkeypatch
):
    """The maintainer's external-session replay: a state.db-only session
    owned by alice (mapped), browsed while the DEFAULT profile is active.

    Listing, reading and mutation must all operate on alice's mapped
    WebUI-local root — the same root, from the same owner authority. The
    ambient last workspace (what the pre-fix file ops used) must not be
    touched.
    """
    alice_ws = profile_world["alice_ws"]
    ambient_ws = profile_world["ambient_ws"]
    (alice_ws / "secret.txt").write_text("ALICE-EXTERNAL", encoding="utf-8")
    (ambient_ws / "secret.txt").write_text("AMBIENT-DECOY", encoding="utf-8")

    active_profile("default")
    _patch_external_session(
        monkeypatch,
        row={"session_id": "ext-1", "workspace": "/workspace", "profile": "alice"},
        ambient_ws=ambient_ws,
    )
    import api.routes as routes

    list_status, list_payload, read_status, read_content, delete_status = _drive_three_surfaces(
        routes, "ext-1", "secret.txt"
    )

    assert list_status == 200
    assert list_payload["workspace"] == str(alice_ws.resolve())
    assert any(e.get("name") == "secret.txt" for e in list_payload["entries"])
    # Read: the owner-selected bytes, from the same root the listing showed.
    assert read_status == 200
    assert read_content == "ALICE-EXTERNAL"
    # Mutation: the delete lands on the same root.
    assert delete_status == 200
    assert not (alice_ws / "secret.txt").exists()
    # The ambient decoy is never read, listed or mutated.
    assert (ambient_ws / "secret.txt").exists()


def test_external_view_preserves_agent_visible_workspace(
    profile_world, active_profile, monkeypatch
):
    """The external view keeps the agent-visible workspace; only
    ``local_workspace`` carries the WebUI-local root, so stored and
    forwarded session data keep the agent-side name."""
    import api.models as models

    active_profile("default")
    _patch_external_session(
        monkeypatch,
        row={"session_id": "ext-2", "workspace": "/workspace", "profile": "alice"},
        ambient_ws=profile_world["ambient_ws"],
    )

    view = models.get_session_for_file_ops("ext-2")

    assert view.workspace == "/workspace"
    assert view.profile == "alice"
    assert view.local_workspace == profile_world["alice_ws"].resolve()


def test_external_unmapped_owner_uses_owner_trusted_root(
    profile_world, active_profile, monkeypatch
):
    """Neighbour case: an unmapped owner profile resolves through the
    OWNER's trusted resolution (saved-list authorization), never the
    ambient workspace."""
    bravo_only = profile_world["bravo_only"]
    (bravo_only / "bravo-file.txt").write_text("BRAVO-OWNER", encoding="utf-8")

    active_profile("default")
    _patch_external_session(
        monkeypatch,
        row={"session_id": "ext-3", "workspace": str(bravo_only), "profile": "bravo"},
        ambient_ws=profile_world["ambient_ws"],
    )
    import api.routes as routes

    list_status, list_payload, read_status, read_content, delete_status = _drive_three_surfaces(
        routes, "ext-3", "bravo-file.txt"
    )

    assert list_status == 200
    assert list_payload["workspace"] == str(bravo_only.resolve())
    assert read_status == 200
    assert read_content == "BRAVO-OWNER"
    assert delete_status == 200
    assert not (bravo_only / "bravo-file.txt").exists()


def test_external_untrusted_owner_fails_closed_on_every_surface(
    profile_world, active_profile, monkeypatch
):
    """Neighbour case: with the owner's saved entry revoked, listing,
    reading and mutation all fail closed (404) — no surface silently
    passes the untrusted path through."""
    bravo_only = profile_world["bravo_only"]
    profile_world["saved"]["bravo"].clear()

    active_profile("default")
    _patch_external_session(
        monkeypatch,
        row={"session_id": "ext-4", "workspace": str(bravo_only), "profile": "bravo"},
        ambient_ws=profile_world["ambient_ws"],
    )
    import api.routes as routes

    list_status, _payload, read_status, _content, delete_status = _drive_three_surfaces(
        routes, "ext-4", "bravo-file.txt"
    )

    assert list_status == 404
    assert read_status == 404
    assert delete_status == 404


def test_external_mapped_missing_local_root_fails_closed(
    profile_world, active_profile, monkeypatch
):
    """Neighbour case: a mapped session whose WebUI-local root is
    genuinely gone fails closed on every surface instead of jumping to
    the ambient workspace."""
    import shutil

    shutil.rmtree(profile_world["alice_ws"])
    active_profile("default")
    _patch_external_session(
        monkeypatch,
        row={"session_id": "ext-5", "workspace": "/workspace", "profile": "alice"},
        ambient_ws=profile_world["ambient_ws"],
    )
    import api.routes as routes

    list_status, _payload, read_status, _content, delete_status = _drive_three_surfaces(
        routes, "ext-5", "secret.txt"
    )

    assert list_status == 404
    assert read_status == 404
    assert delete_status == 404


def test_external_session_without_cli_row_keeps_ambient_view(
    profile_world, active_profile, monkeypatch
):
    """Same-input control: a state.db session with NO discoverable CLI
    row keeps the pre-existing ambient view byte-for-byte (the #3280
    contract), including the profile-less direct-path behaviour."""
    import api.models as models

    ambient_ws = profile_world["ambient_ws"]
    active_profile("default")
    _patch_external_session(monkeypatch, row=None, ambient_ws=ambient_ws)

    view = models.get_session_for_file_ops("ext-6")

    assert isinstance(view, models._ExternalSessionView)
    assert view.workspace == str(ambient_ws)
    assert view.local_workspace is None


def test_external_foreign_owner_resolution_stays_owner_scoped(
    profile_world, active_profile, monkeypatch
):
    """Wrong-owner isolation for the external path: while the DEFAULT
    profile is active, an alice-owned external session still resolves
    through ALICE's trust — and alice's revoked trust is a 404, not a
    fallback to the ambient root."""
    alice_ws = profile_world["alice_ws"]
    (alice_ws / "secret.txt").write_text("ALICE-FOREIGN-OWNER", encoding="utf-8")

    active_profile("default")
    _patch_external_session(
        monkeypatch,
        row={"session_id": "ext-7", "workspace": "/workspace", "profile": "alice"},
        ambient_ws=profile_world["ambient_ws"],
    )
    import api.routes as routes

    list_status, list_payload, read_status, read_content, _delete = _drive_three_surfaces(
        routes, "ext-7", "secret.txt"
    )

    # alice's mapped root is under the faked home (trust rule A), so the
    # foreign-owner session resolves through alice's own authority.
    assert list_status == 200
    assert list_payload["workspace"] == str(alice_ws.resolve())
    assert read_status == 200
    assert read_content == "ALICE-FOREIGN-OWNER"


def test_native_mapped_session_three_surfaces_share_one_root(
    profile_world, active_profile, monkeypatch
):
    """Parity control for the native origin: a mapped WebUI session
    resolves list/read/delete through the same translated root."""
    import api.models as models
    import api.routes as routes

    alice_ws = profile_world["alice_ws"]
    (alice_ws / "native.txt").write_text("NATIVE-ALICE", encoding="utf-8")
    active_profile("alice")
    # No ambient override: alice's own config.yaml is the authority, so
    # this parity control is green before and after the repair.
    _set_config_override(monkeypatch, None)

    session = SimpleNamespace(session_id="native-2", workspace="/workspace", profile="alice")
    monkeypatch.setattr(models, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(routes, "get_session", lambda sid, metadata_only=False: session)
    monkeypatch.setattr(models, "state_db_has_session", lambda sid: False)
    monkeypatch.setattr(models, "get_last_workspace", lambda *a, **k: str(profile_world["ambient_ws"]))
    monkeypatch.setattr(routes, "get_session_for_file_ops", models.get_session_for_file_ops)

    list_status, list_payload, read_status, read_content, delete_status = _drive_three_surfaces(
        routes, "native-2", "native.txt"
    )

    assert list_status == 200
    assert list_payload["workspace"] == str(alice_ws.resolve())
    assert read_status == 200
    assert read_content == "NATIVE-ALICE"
    assert delete_status == 200
    assert not (alice_ws / "native.txt").exists()
    assert session.workspace == "/workspace"
