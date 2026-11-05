"""An unresolved ``${VAR}`` terminal backend is not a remote backend (#8744).

#8737 expands a profile's ``config.yaml`` with that profile's OWN ``.env`` laid
over the normal resolution (decision A): the profile's ``.env`` wins for the
names it defines, other names resolve as on master, and a name nothing defines
stays the literal ``${VAR}``. Downstream consumers must treat such a literal as
ABSENT. ``api/workspace.py::_is_remote_terminal_backend`` used to
classify any non-empty value other than ``local`` as remote, so the kept
placeholder switched on remote-workspace behavior: ``get_last_workspace()``
dropped the profile's saved local workspace and returned ``terminal.cwd``.

Treating the placeholder as absent must not open the two gates that may only
proceed on a backend KNOWN to be local, so these tests also pin that:

* workspace recovery (``resolve_implicit_workspace_with_recovery``) still
  refuses to swap a missing workspace for the fallback, and
* the embedded terminal still refuses to start (and ``/api/workspaces`` still
  reports the terminal as unavailable).

Each deny test has a ``local`` control so it cannot pass vacuously.
"""

import io
import json
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from urllib.parse import urlsplit

import pytest

PROFILE = "gate"

UNRESOLVED_CONFIG = """\
terminal:
  backend: ${GATE_BACKEND}
  cwd: /srv/x
"""


@pytest.fixture
def gate_profile(tmp_path, monkeypatch):
    """A named profile whose ``terminal.backend`` names a variable nothing defines.

    Under the #8737 overlay (decision A) the profile's own ``.env`` wins for the
    names it defines and every other name resolves as on master, process env
    included. So the backend stays the literal ``${GATE_BACKEND}`` only when
    neither the profile's ``.env`` nor the process env defines it, which is
    the case these tests pin.
    """
    import api.paths as paths
    import api.profiles as profiles

    root = tmp_path / ".hermes"
    home = root / "profiles" / PROFILE
    home.mkdir(parents=True)
    (home / ".env").write_text("UNRELATED_KEY=1\n", encoding="utf-8")
    (home / "config.yaml").write_text(UNRESOLVED_CONFIG, encoding="utf-8")

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "default", "is_default": True}, {"name": PROFILE}],
    )
    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType({}), raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    profiles._invalidate_root_profile_cache()
    profiles._tls.profile = None

    # Defined nowhere: absent from the profile's .env and from the process env.
    monkeypatch.delenv("GATE_BACKEND", raising=False)

    yield home

    profiles._tls.profile = None
    profiles._invalidate_root_profile_cache()


def _save_last_workspace(path: Path) -> None:
    from api import workspace

    lw_file = workspace._last_workspace_file(profile=PROFILE)
    lw_file.parent.mkdir(parents=True, exist_ok=True)
    lw_file.write_text(str(path), encoding="utf-8")


# ── Classifier contract ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "backend, remote",
    [
        pytest.param("ssh", True, id="ssh"),
        pytest.param("docker", True, id="docker"),
        pytest.param(" SSH ", True, id="ssh-padded"),
        pytest.param("local", False, id="local"),
        pytest.param("", False, id="empty"),
        pytest.param(None, False, id="none"),
        pytest.param("${GATE_BACKEND}", False, id="unresolved"),
        pytest.param(" ${GATE_BACKEND} ", False, id="unresolved-padded"),
        pytest.param("ssh-${GATE_SUFFIX}", False, id="partly-unresolved"),
    ],
)
def test_remote_classifier_treats_unresolved_backend_as_absent(backend, remote):
    from api.workspace import _is_remote_terminal_backend

    cfg = {"backend": backend, "cwd": "/srv/x"}
    assert _is_remote_terminal_backend(cfg) is remote, (
        f"backend {backend!r}: remote classification should be {remote}"
    )


@pytest.mark.parametrize(
    "backend, unresolved",
    [
        pytest.param("ssh", False, id="ssh"),
        pytest.param("local", False, id="local"),
        pytest.param(None, False, id="none"),
        pytest.param("${GATE_BACKEND}", True, id="unresolved"),
        pytest.param("ssh-${GATE_SUFFIX}", True, id="partly-unresolved"),
    ],
)
def test_unresolved_backend_helper(backend, unresolved):
    from api.workspace import _terminal_backend_is_unresolved

    assert _terminal_backend_is_unresolved({"backend": backend}) is unresolved
    assert _terminal_backend_is_unresolved(None) is False


# ── Positive classification: no remote-workspace behavior ───────────────────


def test_unresolved_backend_keeps_saved_local_workspace(gate_profile, tmp_path):
    """The Gate reproduction: the saved local workspace must win, not /srv/x."""
    from api import workspace
    from api.config import get_config_for_profile_home

    saved = tmp_path / "saved-local"
    saved.mkdir()
    _save_last_workspace(saved)

    # The overlay contract: a name nothing defines stays the literal placeholder.
    terminal_cfg = get_config_for_profile_home(gate_profile)["terminal"]
    assert terminal_cfg["backend"] == "${GATE_BACKEND}"

    assert workspace.get_last_workspace(profile=PROFILE) == str(saved), (
        "an unresolved ${VAR} backend must not replace the saved local workspace"
    )
    assert workspace._remote_terminal_cwd(profile=PROFILE) is None, (
        "an unresolved ${VAR} backend must not expose terminal.cwd as a remote cwd"
    )


def test_unresolved_backend_does_not_return_unchecked_terminal_cwd(
    gate_profile, tmp_path, monkeypatch
):
    """With no saved workspace, terminal.cwd is checked as a LOCAL path."""
    import api.config as api_config
    from api import workspace

    target_only = tmp_path / "target-only"  # never created on this host
    (gate_profile / "config.yaml").write_text(
        f"terminal:\n  backend: ${{GATE_BACKEND}}\n  cwd: {target_only}\n",
        encoding="utf-8",
    )
    live_default = tmp_path / "live-default"
    live_default.mkdir()
    monkeypatch.setattr(api_config, "DEFAULT_WORKSPACE", live_default)

    result = workspace._profile_default_workspace(profile=PROFILE)

    assert result != str(target_only), (
        "an unresolved ${VAR} backend must not return terminal.cwd without a local check"
    )
    assert result == str(live_default.resolve())


# ── Deny gate 1: workspace recovery ─────────────────────────────────────────


def _recover_missing_workspace(tmp_path, monkeypatch):
    from api import workspace

    monkeypatch.setattr(workspace, "_home_path", lambda: tmp_path)
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    calls = []

    def fallback_getter(*args):
        calls.append(args)
        return fallback

    missing = tmp_path / "deleted-workspace"
    return calls, fallback, lambda: workspace.resolve_implicit_workspace_with_recovery(
        str(missing), fallback_getter, profile=PROFILE
    )


def test_workspace_recovery_refuses_unresolved_backend(gate_profile, tmp_path, monkeypatch):
    calls, _fallback, recover = _recover_missing_workspace(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="does not exist"):
        recover()

    assert calls == [], (
        "workspace recovery must not fall back when the backend is an unresolved ${VAR}"
    )


def test_workspace_recovery_control_local_backend_recovers(gate_profile, tmp_path, monkeypatch):
    """Control: the same setup with a RESOLVED local backend does recover.

    The workspace path reads the profile config with master's resolution (the
    #8737 overlay applies to the Gateway config read only), so the name is
    resolved the way master resolves it here: from the process env.
    """
    monkeypatch.setenv("GATE_BACKEND", "local")
    calls, fallback, recover = _recover_missing_workspace(tmp_path, monkeypatch)

    resolved, recovered = recover()

    assert recovered is True
    assert resolved == fallback.resolve()
    assert len(calls) == 1


# ── Deny gate 2: embedded terminal capability ───────────────────────────────


class _RouteHandler:
    def __init__(self):
        self.headers = {}
        # Loopback client so the embedded-terminal local-origin gate admits the
        # request and the backend check below is what decides.
        self.client_address = ("127.0.0.1", 12345)
        self.wfile = io.BytesIO()
        self.responses = []

    def send_response(self, status):
        self.responses.append(status)

    def send_header(self, _name, _value):
        pass

    def end_headers(self):
        pass


def _start_terminal(monkeypatch, tmp_path, backend):
    """Drive ``_handle_terminal_start`` with a stubbed PTY so nothing is spawned."""
    import api.routes as routes
    import api.terminal as terminal

    monkeypatch.setenv("GATE_BACKEND", "local")
    monkeypatch.setattr(
        routes, "get_config", lambda: {"terminal": {"backend": backend, "cwd": "/srv/x"}}
    )
    monkeypatch.setattr(
        routes,
        "get_session",
        lambda sid: SimpleNamespace(session_id=sid, workspace=str(tmp_path), profile=None),
    )
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda *_a, **_k: tmp_path)
    started = []

    def fake_start_terminal(*args, **kwargs):
        started.append((args, kwargs))
        raise RuntimeError("stub: terminal start reached")

    monkeypatch.setattr(terminal, "start_terminal", fake_start_terminal)

    handler = _RouteHandler()
    routes._handle_terminal_start(
        handler, {"session_id": "session-8744", "rows": 24, "cols": 80, "restart": False}
    )
    return handler, started


def test_embedded_terminal_refuses_unresolved_backend(monkeypatch, tmp_path):
    import api.routes as routes

    handler, started = _start_terminal(monkeypatch, tmp_path, "${GATE_BACKEND}")
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))

    assert started == [], "the embedded terminal must not start on an unresolved ${VAR} backend"
    assert handler.responses == [400]
    assert payload["error"] == "remote_terminal_backend_unsupported"
    assert routes._terminal_remote_backend_enabled() is True


def test_embedded_terminal_control_local_backend_reaches_start(monkeypatch, tmp_path):
    """Control: a known-local backend gets past the backend check."""
    import api.routes as routes

    _handler, started = _start_terminal(monkeypatch, tmp_path, "local")

    assert len(started) == 1
    assert routes._terminal_remote_backend_enabled() is False


def test_workspaces_route_reports_terminal_unavailable_for_unresolved_backend(monkeypatch):
    import api.routes as routes

    monkeypatch.setenv("GATE_BACKEND", "local")
    monkeypatch.setattr(routes, "load_workspaces", lambda **_k: [])
    monkeypatch.setattr(routes, "get_last_workspace", lambda **_k: "/tmp/project")
    monkeypatch.setattr(
        routes, "get_config", lambda: {"terminal": {"backend": "${GATE_BACKEND}"}}
    )

    handler = _RouteHandler()
    routes.handle_get(handler, urlsplit("/api/workspaces"))
    payload = json.loads(handler.wfile.getvalue().decode("utf-8"))

    assert handler.responses == [200]
    assert payload["terminal_remote_backend"] is True, (
        "/api/workspaces must report the embedded terminal unavailable on an unresolved ${VAR} backend"
    )
