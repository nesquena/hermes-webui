"""#7905 round 7 — the active owner's environment, and the request producers end to end.

Review 2026-11-24T05:28:26Z (owner-environment, still open):

* ``api/config.py:1014-1015`` returns ``get_config()`` immediately when the
  selected source is ``_AMBIENT_CONFIG``. That bypasses ``own_environment=True``
  even when the selected file IS the requested named owner's own config — which
  is exactly the case while that named profile is also process-active, because
  the source selection aliases the owner's file to "ambient". A normal
  configuration refresh then expands the owner's ``${VAR}`` references under the
  LIVE process env (whichever profile mirrored into it last) and the Gateway
  model text is calculated wrong.
* the session creation/update coverage added in round 6 calls the helpers
  directly and the update drives a synthetic session that is never saved, so the
  request producers and the persisted round trips were never exercised.

Every gateway case below runs the real resolvers and loaders on a NEW worker
thread with no request profile context. The controls are paired: they pass on
both the pre-fix and the post-fix code, so a green control never stands in for
the assertion being measured. ``own_environment`` must keep honouring genuine
in-memory overrides, root/external-override authority and named-profile
isolation — those are the controls, not optional extras.
"""
from __future__ import annotations

import threading
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import MagicMock

import api.config as config
import api.models as models
import api.profiles as profiles
import api.routes as routes

GENERIC_ROOT = (
    "model:\n"
    "  provider: deepseek\n"
    "  default: deepseek-chat\n"
    "providers:\n"
    "  custom:\n"
    "    base_url: https://generic.example/v1\n"
    "  deepseek:\n"
    "    base_url: https://api.deepseek.com/v1\n"
)
OMNI_LITERAL = (
    "providers:\n"
    "  custom:omni:\n"
    "    base_url: https://omni.example/v1\n"
    "    models: [latest]\n"
)
GENERIC_ONLY = "providers:\n  custom:\n    base_url: https://generic.example/v1\n"
# The root profile owns BOTH the generic lane and a named ``custom:omni``, so an
# explicit named pick is a model the default profile really knows.
ROOT_WITH_OMNI = (
    "model:\n"
    "  provider: deepseek\n"
    "  default: deepseek-chat\n"
    "providers:\n"
    "  custom:\n"
    "    base_url: https://generic.example/v1\n"
    "  custom:omni:\n"
    "    base_url: https://omni.example/v1\n"
    "    models: [latest]\n"
    "  deepseek:\n"
    "    base_url: https://api.deepseek.com/v1\n"
)
# The identity travels through the profile's OWN .env: config.yaml keeps the
# ``${VAR}`` reference on disk, exactly as the Agent's launcher writes it.
OMNI_FROM_ENV = (
    "custom_providers:\n"
    "  - name: ${OMNI_SLUG}\n"
    "    base_url: https://omni.example/v1\n"
    "    models: [latest]\n"
)


def _homes(tmp_path, monkeypatch, active="default"):
    """Disposable root home; ``work`` is a named profile under it."""
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (root / "config.yaml").write_text(GENERIC_ROOT, encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    # ``active`` is the PROCESS-ACTIVE profile: for the defect case it is the
    # session's named owner itself, which is what aliases its own file to the
    # ambient source inside ``_profile_home_config_source``.
    monkeypatch.setattr(profiles, "_active_profile", active)
    monkeypatch.setattr(profiles._tls, "profile", None, raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    return root, work


def _in_worker(fn):
    """Run *fn* on a fresh thread with no profile context; return its value."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the test thread
            box["error"] = exc

    thread = threading.Thread(target=run, name="7905-round7-worker")
    thread.start()
    thread.join(60)
    assert not thread.is_alive(), "worker thread did not finish"
    if "error" in box:
        raise box["error"]
    return box["value"]


def _gateway_field(model, provider, profile):
    from api.gateway_chat import _gateway_model_field

    return _gateway_model_field(model, session_provider=provider, profile=profile)


# ── defect: the ACTIVE owner's file must expand under the owner's env ───────
def test_active_owner_environment_is_honored_after_a_refresh(tmp_path, monkeypatch):
    """``work`` is process-active AND the session's profile.

    The live env carries a FOREIGN profile's value (``otherbox``) because the
    worker thread's env is whichever profile mirrored into it last. Only the
    owner's own binding turns ``${OMNI_SLUG}`` into ``omni`` — reading the
    ambient cache here yields the foreign identity and the ``custom:<name>``
    slug is peeled into the generic lane.
    """
    _root, work = _homes(tmp_path, monkeypatch, active="work")
    (work / "config.yaml").write_text(OMNI_FROM_ENV, encoding="utf-8")
    (work / ".env").write_text("OMNI_SLUG=omni\n", encoding="utf-8")
    monkeypatch.setenv("OMNI_SLUG", "otherbox")
    config.reload_config()
    config.get_config()  # a normal refresh leaves the ambient cache warm

    def probe():
        assert profiles.get_active_profile_name() == "work"
        assert getattr(profiles._tls, "profile", None) is None
        return _gateway_field("@custom:omni:latest", "custom", "work")

    assert _in_worker(probe) == "latest"


# ── controls: forms that must keep their current behaviour ─────────────────
def test_control_warm_cache_literal_identity_for_the_active_owner(tmp_path, monkeypatch):
    """A literal owner file needs no expansion; warm cache changes nothing."""
    _root, work = _homes(tmp_path, monkeypatch, active="work")
    (work / "config.yaml").write_text(OMNI_LITERAL, encoding="utf-8")
    config.reload_config()
    config.get_config()

    assert _in_worker(lambda: _gateway_field("@custom:omni:latest", "custom", "work")) == "latest"


def test_control_non_active_owner_environment(tmp_path, monkeypatch):
    """The non-active owner already read its own file + env before this round."""
    _root, work = _homes(tmp_path, monkeypatch, active="default")
    (work / "config.yaml").write_text(OMNI_FROM_ENV, encoding="utf-8")
    (work / ".env").write_text("OMNI_SLUG=omni\n", encoding="utf-8")
    monkeypatch.setenv("OMNI_SLUG", "otherbox")
    config.reload_config()

    assert _in_worker(lambda: _gateway_field("@custom:omni:latest", "custom", "work")) == "latest"


def test_control_in_memory_override_survives_for_the_active_owner(tmp_path, monkeypatch):
    """A rebound ``config.cfg`` override still wins over the file."""
    _root, work = _homes(tmp_path, monkeypatch, active="work")
    (work / "config.yaml").write_text(GENERIC_ONLY, encoding="utf-8")
    config.reload_config()
    override = {
        "model": {"provider": "deepseek", "default": "deepseek-chat"},
        "providers": {"custom": {"base_url": "https://generic.example/v1"},
                      "custom:omni": {"base_url": "https://override.example/v1"}},
    }
    monkeypatch.setattr(config, "cfg", override)

    returned = config.get_config_for_profile_home(
        profiles.get_hermes_home_for_profile("work"), own_environment=True
    )
    assert returned is override
    assert "custom:omni" in returned["providers"]


def test_control_external_override_authority_for_the_root_profile(tmp_path, monkeypatch):
    """The root profile keeps reading an external ``HERMES_CONFIG_PATH``."""
    _root, work = _homes(tmp_path, monkeypatch, active="work")
    (work / "config.yaml").write_text(GENERIC_ONLY, encoding="utf-8")
    external = tmp_path / "mounted" / "config.yaml"
    external.parent.mkdir()
    external.write_text(OMNI_LITERAL, encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    config.reload_config()
    config.get_config()

    for profile in (None, "", "default"):
        assert _in_worker(lambda p=profile: _gateway_field("@custom:omni:latest", "custom", p)) == "latest"


def test_control_named_owner_never_reads_an_external_override(tmp_path, monkeypatch):
    """An override outside ``work``'s home must not become ``work``'s config."""
    _root, work = _homes(tmp_path, monkeypatch, active="work")
    (work / "config.yaml").write_text(GENERIC_ONLY, encoding="utf-8")
    external = tmp_path / "mounted" / "config.yaml"
    external.parent.mkdir()
    external.write_text(OMNI_LITERAL, encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    config.reload_config()

    assert _in_worker(lambda: _gateway_field("@custom:omni:latest", "custom", "work")) == "omni:latest"


# ── request producers: real routes, real persistence ──────────────────────
def _post(path, body, monkeypatch):
    """POST *path* through the real dispatcher; return the captured response."""
    cap = {}

    def _j(_handler, payload, *_, **__):
        cap["ok"] = payload
        return True

    def _bad(_handler, msg, code=400):
        cap["bad"] = (msg, code)
        return True

    handler = MagicMock()
    handler.command = "POST"
    handler.headers = {}
    monkeypatch.setattr(routes, "resolve_trusted_workspace", lambda value, **_kw: Path(value))
    monkeypatch.setattr(routes, "read_body", lambda _h: body)
    monkeypatch.setattr(routes, "_check_csrf", lambda _h: True)
    monkeypatch.setattr(routes, "_csrf_exempt_path", lambda _p: False)
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "j", _j)
    monkeypatch.setattr(routes, "bad", _bad)
    routes.handle_post(handler, urlparse(path))
    return cap


def test_session_creation_route_persists_the_generic_lane(tmp_path, monkeypatch):
    """``POST /api/session/new`` stores ``custom`` and it survives save + reload.

    The producer (``_session_model_state_from_request`` ->
    ``_resolve_compatible_session_model_state``) runs for real inside the
    route: nothing here substitutes the normalizer.
    """
    _root, _work = _homes(tmp_path, monkeypatch)
    config.reload_config()
    workspace = tmp_path / "ws"
    workspace.mkdir()

    cap = _post(
        "/api/session/new",
        {"model": "@custom:omni:latest", "model_provider": "custom", "workspace": str(workspace)},
        monkeypatch,
    )
    assert "bad" not in cap, cap.get("bad")
    session_id = cap["ok"]["session"]["session_id"]

    live = models.SESSIONS.get(session_id)
    assert live is not None
    assert live.model_provider == "custom"
    live.save()
    reloaded = models.Session.load(session_id)
    assert reloaded.model_provider == "custom"
    assert reloaded.model == live.model


def test_session_update_route_persists_the_generic_lane(tmp_path, monkeypatch):
    """``POST /api/session/update`` keeps ``custom`` across a real save + reload.

    A switch that carries only the model inherits the stored provider, so the
    persisted session must not be re-tagged ``custom:omni`` — an identity that
    exists nowhere.
    """
    _root, _work = _homes(tmp_path, monkeypatch)
    config.reload_config()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = models.new_session(
        workspace=str(workspace), model="omni:latest", model_provider="custom", profile=None
    )
    session.save()

    cap = _post(
        "/api/session/update",
        {"session_id": session.session_id, "model": "@custom:omni:latest"},
        monkeypatch,
    )
    assert "bad" not in cap, cap.get("bad")

    reloaded = models.Session.load(session.session_id)
    assert reloaded.model_provider == "custom"
    assert str(reloaded.model or "").endswith("latest")


def test_control_update_with_an_explicit_named_provider_is_stored_verbatim(tmp_path, monkeypatch):
    """An explicit ``custom:omni`` pick keeps its slug through the route."""
    root, _work = _homes(tmp_path, monkeypatch)
    (root / "config.yaml").write_text(ROOT_WITH_OMNI, encoding="utf-8")
    config.reload_config()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session = models.new_session(
        workspace=str(workspace), model="omni:latest", model_provider="custom", profile=None
    )
    session.save()

    cap = _post(
        "/api/session/update",
        {
            "session_id": session.session_id,
            "model": "@custom:omni:latest",
            "model_provider": "custom:omni",
        },
        monkeypatch,
    )
    assert "bad" not in cap, cap.get("bad")

    reloaded = models.Session.load(session.session_id)
    assert reloaded.model_provider == "custom:omni"
