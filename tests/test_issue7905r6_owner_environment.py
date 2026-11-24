"""#7905 round 6 — the SESSION owner's environment, and the producers that drop it.

Gate certificate 2026-11-23: ``_gateway_profile_config`` read the session
profile's ``config.yaml`` (file authority, round 1) but expanded its ``${VAR}``
references under the LIVE process env. On a detached Gateway worker thread that
env belongs to whichever profile mirrored into it last, so an identity only the
owner's ``.env`` binds (``custom_providers: [{name: ${OMNI_SLUG}}]``) expanded
to a foreign name — or stayed unresolved — and the ``custom:<name>`` slug was
peeled into the generic lane. The owner has BOTH authorities: its file and its
environment (``api.profiles.profile_env_binding`` / ``load_profile_config``).

The follow-up finding was #7904's producer half: ``_session_model_state_from_request``
split ``@custom:<seg>:<tag>`` with no lane evidence and let the bare parse
overwrite the requested generic ``custom`` provider BEFORE the (already fixed)
resolver ran, so session creation and model switches persisted ``custom:<seg>``
— a provider identity that exists nowhere.

Every gateway case below runs the real resolvers and loaders on a NEW worker
thread with no request profile context. The controls are paired: they pass on
both the pre-fix and post-fix code, so a green control never stands in for the
assertion being measured.
"""
import threading

from api import config, profiles

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
OMNI_PROVIDER = (
    "providers:\n"
    "  custom:omni:\n"
    "    base_url: https://omni.example/v1\n"
    "    models: [latest]\n"
)
GENERIC_ONLY = "providers:\n  custom:\n    base_url: https://generic.example/v1\n"
# The identity travels through the profile's OWN .env: config.yaml keeps the
# ``${VAR}`` reference on disk, exactly as the Agent's launcher writes it.
OMNI_FROM_ENV = (
    "custom_providers:\n"
    "  - name: ${OMNI_SLUG}\n"
    "    base_url: https://omni.example/v1\n"
    "    models: [latest]\n"
)


def _homes(tmp_path, monkeypatch):
    """Disposable root home; ``work`` is a named profile under it."""
    root = tmp_path / "root"
    work = root / "profiles" / "work"
    work.mkdir(parents=True)
    (root / "config.yaml").write_text(GENERIC_ROOT, encoding="utf-8")
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    # No request profile context: the process-active profile is the root one,
    # and no TLS request context is set (the Gateway worker's real posture).
    monkeypatch.setattr(profiles, "_active_profile", "default")
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

    thread = threading.Thread(target=run, name="7905-round6-worker")
    thread.start()
    thread.join(60)
    assert not thread.is_alive(), "worker thread did not finish"
    if "error" in box:
        raise box["error"]
    return box["value"]


def _gateway_field(model, provider, profile):
    from api.gateway_chat import _gateway_model_field

    return _gateway_model_field(model, session_provider=provider, profile=profile)


# ── case 1: identity lives in the owner's FILE, not in the ambient config ───
def test_named_profile_file_identity_survives_the_process_env(tmp_path, monkeypatch):
    """``work`` declares ``custom:omni``; the root/ambient config never does."""
    _root, work = _homes(tmp_path, monkeypatch)
    (work / "config.yaml").write_text(OMNI_PROVIDER, encoding="utf-8")
    config.reload_config()

    def probe():
        # Real resolvers, real loader, no request profile context.
        assert profiles.get_active_profile_name() == "default"
        assert getattr(profiles._tls, "profile", None) is None
        return _gateway_field("@custom:omni:latest", "custom", "work")

    assert _in_worker(probe) == "latest"


# ── case 2: the SAME identity, bound by the owner's ENVIRONMENT (§ gate) ────
def test_named_profile_environment_identity_is_resolved(tmp_path, monkeypatch):
    """``custom:${OMNI_SLUG}`` resolves under ``work/.env``, not the live env.

    The live env carries a FOREIGN profile's value (``otherbox``): the exact
    detached-worker posture the gate reproduced. Only the owner's binding can
    turn ``${OMNI_SLUG}`` into ``omni``.
    """
    _root, work = _homes(tmp_path, monkeypatch)
    (work / "config.yaml").write_text(OMNI_FROM_ENV, encoding="utf-8")
    (work / ".env").write_text("OMNI_SLUG=omni\n", encoding="utf-8")
    monkeypatch.setenv("OMNI_SLUG", "otherbox")
    config.reload_config()

    assert (
        _in_worker(lambda: _gateway_field("@custom:omni:latest", "custom", "work"))
        == "latest"
    )


# ── control: an explicit, literal owner selection is identical on both sides ─
def test_control_explicit_named_selection_is_unchanged(tmp_path, monkeypatch):
    """A session pinned to the named lane keeps ``latest`` on old and new code."""
    _root, work = _homes(tmp_path, monkeypatch)
    (work / "config.yaml").write_text(OMNI_PROVIDER, encoding="utf-8")
    config.reload_config()

    assert (
        _in_worker(
            lambda: _gateway_field("@custom:omni:latest", "custom:omni", "work")
        )
        == "latest"
    )


# ── control: root keeps the external override; named profiles ignore it ─────
def test_control_root_profile_uses_the_external_override(tmp_path, monkeypatch):
    """The root profile (and its aliases) still reads the external override."""
    _root, work = _homes(tmp_path, monkeypatch)
    (work / "config.yaml").write_text(GENERIC_ONLY, encoding="utf-8")
    external = tmp_path / "mounted" / "config.yaml"
    external.parent.mkdir()
    external.write_text(OMNI_PROVIDER, encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    config.reload_config()

    for profile in (None, "", "default"):
        assert (
            _in_worker(lambda p=profile: _gateway_field("@custom:omni:latest", "custom", p))
            == "latest"
        )


def test_control_named_profile_ignores_an_external_override(tmp_path, monkeypatch):
    """An override outside ``work``'s home must not win for ``work``."""
    _root, work = _homes(tmp_path, monkeypatch)
    (work / "config.yaml").write_text(GENERIC_ONLY, encoding="utf-8")
    external = tmp_path / "mounted" / "config.yaml"
    external.parent.mkdir()
    external.write_text(OMNI_PROVIDER, encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(external))
    config.reload_config()

    assert (
        _in_worker(lambda: _gateway_field("@custom:omni:latest", "custom", "work"))
        == "omni:latest"
    )


# ── producers: session creation and model updates keep the lane evidence ────
def _producer_homes(tmp_path, monkeypatch):
    root, work = _homes(tmp_path, monkeypatch)
    config.reload_config()
    return root, work


def test_session_creation_keeps_the_generic_lane_evidence(tmp_path, monkeypatch):
    """``POST /api/session/new`` with a generic colon-tagged model keeps ``custom``.

    The bare parse must not overwrite the requested generic provider with
    ``custom:omni`` — a provider that exists nowhere — before the resolver runs.
    """
    _producer_homes(tmp_path, monkeypatch)
    from api.routes import _session_model_state_from_request

    model, provider = _session_model_state_from_request(
        "@custom:omni:latest", "custom"
    )
    assert provider == "custom"
    route = config.resolve_model_provider(
        config.model_with_provider_context(model, provider), session_provider=provider
    )
    assert route[:2] == ("omni:latest", "custom")


def test_session_update_keeps_the_generic_lane_evidence(tmp_path, monkeypatch):
    """A model switch that carries only the model inherits the stored ``custom``."""
    _producer_homes(tmp_path, monkeypatch)
    from api import routes

    class _Session:
        model = "omni:latest"
        model_provider = "custom"
        last_used_model = None
        gateway_routing = None
        context_length = 0
        threshold_tokens = 0
        last_prompt_tokens = 0

    session = _Session()
    monkeypatch.setattr(
        routes, "_resolve_context_length_for_session_model", lambda *a, **k: 0
    )
    routes._invalidate_session_route_state_if_changed(
        session, model="@custom:omni:latest", provider_specified=False
    )
    assert session.model_provider == "custom"


def test_control_explicit_named_creation_still_stores_its_slug(tmp_path, monkeypatch):
    """An explicit named pick (``custom:omni``) is stored verbatim on both sides."""
    root, _work = _producer_homes(tmp_path, monkeypatch)
    (root / "config.yaml").write_text(
        "model:\n"
        "  provider: deepseek\n"
        "  default: deepseek-chat\n"
        "providers:\n"
        "  custom:\n"
        "    base_url: https://generic.example/v1\n"
        "  custom:omni:\n"
        "    base_url: https://omni.example/v1\n"
        "    models: [latest]\n",
        encoding="utf-8",
    )
    config.reload_config()
    from api.routes import _session_model_state_from_request

    model, provider = _session_model_state_from_request(
        "@custom:omni:latest", "custom:omni"
    )
    assert model == "@custom:omni:latest"
    assert provider == "custom:omni"


def test_control_stored_generic_session_is_not_rerouted(tmp_path, monkeypatch):
    """Compatible-session normalization leaves a stored generic session alone."""
    _producer_homes(tmp_path, monkeypatch)
    from api.routes import _resolve_compatible_session_model_state

    model, provider, changed = _resolve_compatible_session_model_state(
        "omni:latest", "custom"
    )
    assert (model, provider, changed) == ("omni:latest", "custom", False)
