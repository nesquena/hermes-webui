"""Regression coverage for issue #7611: per-deployment instance
label distinguishes multi-instance browser tabs and desktop
windows. The label is installation-scoped, sourced from the env
var ``HERMES_WEBUI_INSTANCE_NAME`` or ``config.yaml``'s
``instance_name`` / ``webui.instance_name``, and exposed via
``GET /api/settings`` as ``settings["instance_label"]`` so the
WebUI can prefix the title without overloading the assistant
name or the profile identity.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


def _load_routes():
    # Import lazily so test collection stays cheap; the module is
    # the same one already loaded by sibling tests, so this is a
    # cache hit after the first call.
    import api.routes as routes
    return routes


def _set_installation_snapshot(monkeypatch, **overrides) -> None:
    """Express the test's operator env through the STARTUP snapshot.

    #7655 round 7: ``_installation_config_path()`` resolves
    ``HERMES_CONFIG_PATH`` from the installation snapshot -- the operator env
    captured once at the startup boundary, before any profile writer ran.
    A value written into ``os.environ`` AFTER that boundary is, by design,
    not the operator's: it is a profile writer's value, and the installation
    resolver must not follow it. So a test that wants an operator value
    records it in the snapshot, exactly as the ``installation_env`` fixture
    does when it drives the real boundary.

    ``None`` removes a key, which is how a test asks for "the operator never
    set this".
    """
    import api.profiles as prof

    snap = dict(getattr(prof, "_INSTALLATION_ENV_SNAPSHOT", {}) or {})
    for key, value in overrides.items():
        if value is None:
            snap.pop(key, None)
        else:
            snap[key] = str(value)
    monkeypatch.setattr(prof, "_INSTALLATION_ENV_SNAPSHOT", snap)


def _install_config(monkeypatch, tmp_path: Path, config: dict) -> Path:
    """Write an INSTALLATION-scoped config.yaml and point the resolver at it.

    The helper under test must never read the request profile's config, so
    these tests pin the installation config via ``HERMES_CONFIG_PATH`` (the
    deployment-level override the whole config layer honours). Writing a real
    file also means the race-safe disk read is genuinely exercised rather
    than a monkeypatched in-memory dict.

    The path is recorded BOTH in the live env and in the installation
    snapshot: the live env is what the ambient config layer honours, and the
    snapshot is what the installation-scoped resolver reads (the operator's
    startup value).
    """
    path = tmp_path / "installation-config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(path))
    _set_installation_snapshot(monkeypatch, HERMES_CONFIG_PATH=str(path))
    return path


def _purge_settings_cache():
    """``/api/settings`` calls ``load_settings()`` which may cache.
    We don't need a fresh load for these tests — we just need
    ``_read_instance_label()`` to re-evaluate env / config on
    every call. The helper itself reads the installation config
    off disk on each invocation, so no module-level cache (in
    ``api.routes`` or ``api.config``) needs clearing."""
    pass


def _snapshot_agent_process_home_pin():
    """Snapshot the Agent's process-home pin, if the Agent package is present.

    ``api.profiles._pin_process_profile_home()`` calls
    ``hermes_constants.pin_process_hermes_home()``, which sets a
    process-global. The Agent package is not installed in every test
    environment, so this returns ``None`` when it is absent.
    """
    try:
        import hermes_constants
    except Exception:
        return None
    pinned = getattr(hermes_constants, "_PINNED_PROCESS_HERMES_HOME", None)
    pin = getattr(hermes_constants, "pin_process_hermes_home", None)
    if not callable(pin):
        return None
    return (pin, pinned)


def _restore_agent_process_home_pin(snapshot) -> None:
    """Restore the Agent's process-home pin captured earlier."""
    if not snapshot:
        return
    pin, pinned = snapshot
    try:
        pin(pinned)
    except Exception:
        pass


def _snapshot_cron_home_modules() -> dict:
    """Snapshot the cron modules' cached home paths.

    ``_set_hermes_home()`` patches ``cron.jobs`` and ``cron.scheduler`` with
    module-level path constants. Those are process state, so a test that
    boots into a temporary home must put them back — otherwise a later test
    reads a home that tmp_path has already deleted.
    """
    snapshot: dict = {}
    for module_name, attrs in (
        ("cron.jobs", ("HERMES_DIR", "CRON_DIR", "JOBS_FILE", "OUTPUT_DIR")),
        ("cron.scheduler", ("_hermes_home", "_LOCK_DIR", "_LOCK_FILE")),
    ):
        try:
            module = __import__(module_name, fromlist=["_"])
        except Exception:
            continue
        for attr in attrs:
            snapshot[(module_name, attr)] = (
                module, attr, hasattr(module, attr), getattr(module, attr, None),
            )
    return snapshot


def _restore_cron_home_modules(snapshot: dict) -> None:
    """Restore the cron modules' cached home paths captured earlier."""
    for (module_name, attr), (module, _attr, had_attr, value) in snapshot.items():
        try:
            if had_attr:
                setattr(module, attr, value)
            else:
                delattr(module, attr)
        except (AttributeError, TypeError):
            pass


def _snapshot_process_home_state(prof) -> dict:
    """Snapshot every piece of process state the startup boundary mutates.

    ``_set_hermes_home()`` — the real call the fixture's ``boot()`` makes —
    mutates five process-global things: the ``HERMES_HOME`` env var, the
    process-profile home pin, the imported skill modules' cached paths, the
    cron modules' cached paths, and the Agent's process-home pin. All of them
    point at a ``tmp_path`` home that is deleted with the test, so a teardown
    that leaves any of them behind makes a later test run against a deleted
    home.
    """
    return {
        "hermes_home": os.environ.get("HERMES_HOME"),
        "had_hermes_home": "HERMES_HOME" in os.environ,
        "process_profile_home": getattr(prof, "_PROCESS_PROFILE_HOME", None),
        "skill_home": prof.snapshot_skill_home_modules(),
        "cron_home": _snapshot_cron_home_modules(),
        "agent_pin": _snapshot_agent_process_home_pin(),
    }


def _restore_process_home_state(prof, snapshot: dict) -> None:
    """Restore the process state captured by ``_snapshot_process_home_state``."""
    if snapshot["had_hermes_home"]:
        os.environ["HERMES_HOME"] = snapshot["hermes_home"] or ""
    else:
        os.environ.pop("HERMES_HOME", None)
    prof._PROCESS_PROFILE_HOME = snapshot["process_profile_home"]
    prof.restore_skill_home_modules(snapshot["skill_home"])
    _restore_cron_home_modules(snapshot["cron_home"])
    _restore_agent_process_home_pin(snapshot["agent_pin"])


@pytest.fixture
def installation_env(monkeypatch, tmp_path):
    """Drive the REAL startup boundary, then isolate every global it touched.

    #7655 round 6: installation placeholders resolve only from the snapshot
    ``api.profiles.capture_installation_env_snapshot`` takes at the startup
    boundary, so a test's operator values must go through that same boundary
    rather than being written into ``os.environ`` afterwards — a value injected
    after the snapshot is, by design, not the operator's.

    The fixture snapshots/restores both loader globals (``_loaded_profile_env_
    keys``, ``_profile_overridden_env``, ``_INSTALLATION_ENV_SNAPSHOT``) plus
    every env key the case touches, so no case can influence another through
    process state.
    """
    import api.profiles as prof

    saved_globals = {
        "_loaded_profile_env_keys": set(getattr(prof, "_loaded_profile_env_keys", set())),
        "_profile_overridden_env": dict(getattr(prof, "_profile_overridden_env", {})),
        "_INSTALLATION_ENV_SNAPSHOT": dict(getattr(prof, "_INSTALLATION_ENV_SNAPSHOT", {})),
    }
    touched = ("SLOT_NAME", "OPERATOR_SLOT", "UNRELATED_PROFILE_KEY", "DUP_SLOT")
    saved_env = {k: os.environ.get(k) for k in touched}
    # `_set_hermes_home()` (called by boot()) mutates five pieces of PROCESS
    # state, not just the loader globals: the HERMES_HOME env var, the
    # process-profile home pin, the imported skill modules' cached paths, the
    # cron modules' cached paths, and the Agent's process-home pin. The
    # teardown used to restore none of them, so a later test could run
    # against a deleted tmp_path home.
    home_state = _snapshot_process_home_state(prof)

    # Clear the snapshot so the boundary can be driven per case.
    prof._INSTALLATION_ENV_SNAPSHOT = {}

    class _Boundary:
        def __init__(self, base: Path):
            self.base = base

        def profile(self, name: str) -> Path:
            """Create (idempotently) a named profile home under the base."""
            d = self.base / "profiles" / name
            d.mkdir(parents=True, exist_ok=True)
            return d

        def boot(self, operator_env: dict | None = None) -> Path:
            """Set the launcher's env, then run the startup boundary.

            Returns the profile home the boundary loaded, so a case can then
            hand a *different* profile to ``_reload_dotenv`` exactly as a
            real profile switch does.

            The capture/reload ORDER is the boundary contract: the snapshot
            must be taken before the first projection, otherwise it enshrines
            whatever the profile installed. ``boot`` performs both, so a test
            that drives the production ``init_profile_state`` order is what
            pins it -- see ``test_boundary_captures_before_the_first_profile``.
            """
            for k in touched:
                monkeypatch.delenv(k, raising=False)
            for k, v in (operator_env or {}).items():
                monkeypatch.setenv(k, v)
            home = self.base
            prof._set_hermes_home(home)
            # The real startup boundary: capture BEFORE the first projection.
            # The snapshot is cleared first because production runs this in a
            # FRESH process, where it is still empty -- and
            # capture_installation_env_snapshot() refuses to re-capture over a
            # non-empty one. Without the clear, a value a test wrote into the
            # snapshot before boot() would make the capture a no-op and the
            # operator's launch env would never be recorded.
            prof._INSTALLATION_ENV_SNAPSHOT = {}
            prof.capture_installation_env_snapshot()
            prof._reload_dotenv(home)
            return home

        def source_order_is_capture_then_reload(self) -> bool:
            """True when the production boundary captures before its first load.

            Reads ``api/profiles.py`` for the real ``init_profile_state`` body
            and checks the ordering in place. A regression that swaps the two
            calls is invisible to any case that drives ``boot``, because
            ``boot`` performs the sequence itself.
            """
            import inspect

            src = inspect.getsource(prof.init_profile_state)
            cap = src.index("capture_installation_env_snapshot()")
            reload_at = src.index("_reload_dotenv(")
            return cap < reload_at

    base = tmp_path / "hermes-home"
    profiles_dir = base / "profiles"
    profiles_dir.mkdir(parents=True)
    yield _Boundary(base)

    prof._loaded_profile_env_keys = saved_globals["_loaded_profile_env_keys"]
    prof._profile_overridden_env = saved_globals["_profile_overridden_env"]
    prof._INSTALLATION_ENV_SNAPSHOT = saved_globals["_INSTALLATION_ENV_SNAPSHOT"]
    # Restore the process-home pin, the skill modules' cached paths, the cron
    # modules' cached paths and the Agent's process-home pin, so a later test
    # cannot inherit the temporary home this case booted into (it is deleted
    # with tmp_path).
    _restore_process_home_state(prof, home_state)
    for k, v in saved_env.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)
    for k in touched:
        os.environ.pop(k, None)


# ── _read_instance_label: env precedence ──────────────────────────────────────


def test_read_instance_label_env_var_beats_installation_config(monkeypatch, tmp_path):
    """The env var is the primary source — it lets operators
    distinguish Production / Staging / Dev without editing
    config.yaml. Order of precedence is env > config > empty,
    so the env value always wins when set."""
    import api.routes as routes
    monkeypatch.setenv("HERMES_WEBUI_INSTANCE_NAME", "Production")
    _install_config(monkeypatch, tmp_path, {"instance_name": "ConfigShouldBeIgnored"})
    assert routes._read_instance_label() == "Production", (
        "env var must take precedence over config.yaml so operators "
        "can override without editing the file"
    )


def test_read_instance_label_strips_whitespace(monkeypatch):
    """The env var is allowed to have surrounding whitespace from
    shell quoting or env-file editors; the helper trims so
    ``'   Production  '`` does not render as ``'   Production  •
    Hermes'`` in the tab title."""
    import api.routes as routes
    monkeypatch.setenv("HERMES_WEBUI_INSTANCE_NAME", "   Staging   ")
    assert routes._read_instance_label() == "Staging"


def test_read_instance_label_falls_back_to_config_top_level(monkeypatch, tmp_path):
    """When the env var is unset, the helper falls back to
    ``config.yaml``'s top-level ``instance_name`` key. This
    matches the configuration shape suggested in the issue body."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    _install_config(monkeypatch, tmp_path, {"instance_name": "Dev"})
    assert routes._read_instance_label() == "Dev"


def test_read_instance_label_falls_back_to_config_nested(monkeypatch, tmp_path):
    """The helper also accepts ``webui.instance_name`` because
    some operators prefer a sectioned config layout. The
    nested key is only consulted when the top-level key is
    absent or empty."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    _install_config(monkeypatch, tmp_path, {"webui": {"instance_name": "Staging"}})
    assert routes._read_instance_label() == "Staging"


def test_boundary_captures_before_the_first_profile(installation_env):
    """The production startup boundary must capture BEFORE projecting.

    The snapshot's whole value is that no profile value can be in it. If
    ``init_profile_state`` captured after its first ``_reload_dotenv``, every
    profile-adjacent name would be enshrined as an "operator" value and the
    leak would be permanent for the process's lifetime -- with no test able to
    see it through ``boot``, which performs the sequence itself.
    """
    assert installation_env.source_order_is_capture_then_reload(), (
        "init_profile_state must capture the installation env BEFORE its "
        "first _reload_dotenv, or the snapshot enshrines profile injection"
    )


def test_read_instance_label_expands_env_placeholder(installation_env, monkeypatch, tmp_path):
    """A ``${VAR}`` placeholder in ``instance_name`` must resolve to the
    variable's value, exactly like every other config read does
    (``api.config._expand_env_vars``).

    A raw YAML read leaves the reference untouched, so an operator deploying
    with ``instance_name: ${DEPLOYMENT_NAME}`` gets the literal
    ``${DEPLOYMENT_NAME}`` in the browser tab instead of their deployment
    name — the placeholder is a deployment-time idiom, and the title is where
    the user actually sees it.

    #7655 round 6: the value comes from the launcher's env as captured at the
    startup boundary, so the operator's variable goes through ``boot()``.
    """
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    _install_config(
        monkeypatch,
        tmp_path,
        {
            "webui": {
                "instance_name": "Deployment-${HERMES_TEST_DEPLOYMENT_NAME}",
            }
        },
    )
    installation_env.boot(operator_env={"HERMES_TEST_DEPLOYMENT_NAME": "Prod-East"})

    label = routes._read_instance_label()
    assert label == "Deployment-Prod-East", (
        f"placeholder must be expanded to the variable's value, got {label!r}"
    )
    assert "${" not in label, (
        f"the raw placeholder must never reach the title, got {label!r}"
    )


def test_read_instance_label_keeps_unresolvable_placeholder_literal(monkeypatch, tmp_path):
    """An UNDEFINED variable is left as the literal reference rather than
    expanded to an empty string — a label that silently became
    ``"Deployment-"`` would look like a misconfigured title, whereas the
    placeholder at least tells the operator which variable failed to resolve.
    This also pins that expansion never raises out of the helper.
    """
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.delenv("HERMES_TEST_DEFINITELY_UNSET", raising=False)
    _install_config(
        monkeypatch,
        tmp_path,
        {"instance_name": "Build-${HERMES_TEST_DEFINITELY_UNSET}"},
    )
    label = routes._read_instance_label()
    assert label == "Build-${HERMES_TEST_DEFINITELY_UNSET}"


def test_read_instance_label_returns_empty_when_unset(monkeypatch, tmp_path):
    """When neither env var nor config keys are set, the helper
    returns the empty string. The frontend treats the empty
    string as "no label" and leaves the default title
    untouched — this is the contract that keeps single-instance
    deployments behaviorally identical to before this PR."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    _install_config(monkeypatch, tmp_path, {"instance_name": "", "webui": {}})
    assert routes._read_instance_label() == "", (
        "empty/unset must round-trip as empty so the frontend can "
        "branch on length without a separate sentinel"
    )


def test_read_instance_label_handles_config_exception(monkeypatch, tmp_path):
    """A malformed installation config.yaml must degrade to the empty
    default rather than raise — ``/api/settings`` must never 500
    because of a broken config. Read off disk (not via the
    ambient ``get_config()`` cache), so this exercises the real
    parse-failure path of the race-safe reader."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    bad = tmp_path / "installation-config.yaml"
    bad.write_text("instance_name: [unclosed\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_CONFIG_PATH", str(bad))
    _set_installation_snapshot(monkeypatch, HERMES_CONFIG_PATH=str(bad))
    assert routes._read_instance_label() == "", (
        "a config read failure must degrade to the empty default; "
        "/api/settings should not 500 because of a broken config"
    )


def test_read_instance_label_rejects_non_string_config_values(monkeypatch, tmp_path):
    """A non-string ``instance_name`` (int, dict, list) must
    not be coerced; the helper skips it and tries the next
    candidate. A future operator who accidentally sets
    ``instance_name: 42`` should not see ``'42 • Hermes'`` in
    their tab title."""
    import api.routes as routes
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    _install_config(
        monkeypatch, tmp_path, {"instance_name": 42, "webui": {"instance_name": ["bad", "shape"]}}
    )
    assert routes._read_instance_label() == "", (
        "non-string candidates must be skipped so a malformed "
        "config value cannot become a tab-title fragment"
    )


def test_read_instance_label_ignores_request_profile_config(monkeypatch, tmp_path):
    """Re-gate finding 1: the label must be INSTALLATION-scoped, so a
    request profile's ``config.yaml`` must never be consulted — not
    even indirectly through the ambient ``get_config()``, which
    resolves the active (request) profile's file and is exactly the
    profile-scoped lookup the reviewer reproduced (default profile
    → "Deployment", named profile → "AliceProfile").

    Here the installation config carries the deployment label and the
    *active profile's* home carries a different one. Whatever the
    active profile resolves to, the helper must return the
    installation value."""
    import api.routes as routes
    import api.profiles as profiles

    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)

    base = tmp_path / "hermes-home"
    alice = base / "profiles" / "alice"
    alice.mkdir(parents=True)
    (base / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "Deployment"}), encoding="utf-8"
    )
    (alice / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "AliceProfile"}), encoding="utf-8"
    )

    # Point the profiler at the temp installation while the request profile
    # is a named one, and make HERMES_CONFIG_PATH ambient-free so resolution
    # has to go through the base-home path.
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    _set_installation_snapshot(monkeypatch, HERMES_CONFIG_PATH=None)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)
    monkeypatch.setattr(profiles, "_active_profile", "alice")

    assert routes._read_instance_label() == "Deployment", (
        "the label must come from the installation/base Hermes home, never from "
        "the active request profile's config.yaml"
    )


def test_read_instance_label_stable_across_profiles(monkeypatch, tmp_path):
    """Re-gate finding 1 (the actual reported symptom): on one
    installation, the default profile and every named profile must
    agree on the label. Each profile here declares its OWN
    ``instance_name``; the installation root's value is the only one
    that may survive, so both resolutions must return it."""
    import api.routes as routes
    import api.profiles as profiles

    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)

    base = tmp_path / "hermes-home"
    for name in ("default-home", "alice", "bob"):
        (base / "profiles" / name).mkdir(parents=True)
    (base / "config.yaml").write_text(
        yaml.safe_dump({"webui": {"instance_name": "Production"}}), encoding="utf-8"
    )
    (base / "profiles" / "alice" / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "AliceProfile"}), encoding="utf-8"
    )
    (base / "profiles" / "bob" / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "BobProfile"}), encoding="utf-8"
    )

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    _set_installation_snapshot(monkeypatch, HERMES_CONFIG_PATH=None)
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)

    labels = {}
    for name in ("default", "alice", "bob"):
        monkeypatch.setattr(profiles, "_active_profile", name)
        labels[name] = routes._read_instance_label()

    assert labels == {"default": "Production", "alice": "Production", "bob": "Production"}, (
        "every profile on one installation must resolve the SAME label; "
        f"got {labels} — the label is profile-scoped again"
    )


def test_read_instance_label_env_var_beats_profile_env_override(monkeypatch, tmp_path):
    """Re-gate finding 2: a profile's own ``.env`` must not be able to
    rewrite the deployment label. ``_reload_dotenv()`` loads the
    active profile's ``.env`` into ``os.environ`` (and that value is
    what ``_read_instance_label()`` reads first), so the deployment
    label has to survive the profile ``.env`` load — otherwise
    whichever profile loads last wins."""
    import api.routes as routes

    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    _set_installation_snapshot(monkeypatch, HERMES_CONFIG_PATH=None)

    base = tmp_path / "hermes-home"
    alice = base / "profiles" / "alice"
    alice.mkdir(parents=True)
    (base / "config.yaml").write_text(
        yaml.safe_dump({"instance_name": "Deployment"}), encoding="utf-8"
    )
    # Alice's profile .env tries to claim the deployment label for herself.
    (alice / ".env").write_text(
        "HERMES_WEBUI_INSTANCE_NAME=AliceProfileEnv\nSOME_OTHER_KEY=ok\n", encoding="utf-8"
    )

    import api.profiles as profiles

    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", base)

    # The loader records what it loaded in a MODULE-LEVEL set and pops every
    # one of those keys on the next call — so a test that leaves it polluted
    # lets a later profile reload delete environment values belonging to
    # another test (or to the server). Snapshot and restore both halves: the
    # keys it tracked, and the env values it overwrote.
    prev_loaded = profiles._loaded_profile_env_keys
    preexisting = {
        k: os.environ.get(k)
        for k in ("HERMES_WEBUI_INSTANCE_NAME", "SOME_OTHER_KEY")
    }

    profiles._reload_dotenv(alice)

    try:
        assert os.environ.get("SOME_OTHER_KEY") == "ok", (
            "non-protected profile .env keys must still load normally"
        )
        assert os.environ.get("HERMES_WEBUI_INSTANCE_NAME") != "AliceProfileEnv", (
            "a profile .env must not be able to override the deployment-level instance label"
        )
        assert routes._read_instance_label() == "Deployment", (
            "the deployment label must survive a profile .env load"
        )
    finally:
        # Restore the exact prior state so this test cannot influence any
        # other test through the loader's global (greptile P2).
        profiles._loaded_profile_env_keys = prev_loaded
        for k, v in preexisting.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_profile_env_cannot_override_protected_instance_label_via_runtime_env(tmp_path):
    """Re-gate finding 2, second door: ``get_profile_runtime_env()`` projects
    a profile's ``.env`` onto the background worker's environment. The
    deployment label must not ride along there either, or the worker sees
    a per-profile label."""
    import api.profiles as profiles

    alice = tmp_path / "profiles" / "alice"
    alice.mkdir(parents=True)
    (alice / ".env").write_text(
        "HERMES_WEBUI_INSTANCE_NAME=AliceProfileEnv\nMY_PROFILE_KEY=value1\n", encoding="utf-8"
    )
    env = profiles.get_profile_runtime_env(alice)
    assert "HERMES_WEBUI_INSTANCE_NAME" not in env, (
        "the runtime env must not carry the deployment instance label out of a profile .env"
    )
    assert env.get("MY_PROFILE_KEY") == "value1"


# ── frontend wiring ──────────────────────────────────────────────────────────


def test_instance_label_round_trips_through_window_global():
    """The boot path must read ``s.instance_label`` from the
    ``/api/settings`` response and pin it on
    ``window._instanceLabel`` so the synchronous ``applyBotName``
    and the later ``syncTopbar`` calls can both prefix the
    title without re-reading the settings payload."""
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    assert "window._instanceLabel=(s.instance_label||'').trim();" in src, (
        "boot.js must read s.instance_label from /api/settings and "
        "expose it on window._instanceLabel"
    )


def test_apply_bot_name_prefixes_with_instance_label():
    """``applyBotName`` must consume ``window._instanceLabel``
    when writing ``document.title`` for the no-session branch
    so the empty-state tab title reflects the deployment."""
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "applyBotName")
    assert "window._instanceLabel" in body, (
        "applyBotName must consult window._instanceLabel"
    )
    assert "_instanceLabel+' \\u2022 '+document.title" in body or (
        "_instanceLabel+' • '+document.title"
    ) in body, (
        "applyBotName must prefix the document.title with "
        "'<label> • <name>' so the empty-state tab is distinguishable"
    )
    # The prefix is applied in-place after the bare title is
    # written, so the same code path covers empty and non-empty
    # labels.
    assert body.count("_instanceLabel") >= 2, (
        "the prefix branch must consult _instanceLabel so the "
        "empty and non-empty cases share the same code path"
    )


def test_apply_bot_name_prefix_is_guarded_by_no_session():
    """Re-gate finding 3: the prefix must live INSIDE the ``!S.session``
    branch.

    ``applyBotName()`` runs on every boot and profile switch, but while a
    chat session is open ``syncTopbar()`` is the sole owner of
    ``document.title`` (#4086). An unconditional prefix re-reads the
    title some other owner already composed and stacks on top of it, so
    two calls turn
    ``Production • Chat — Hermes`` into
    ``Production • Production • Production • Chat — Hermes``.

    Static lock on the shape: the prefix statement's line must be nested
    in the same block that writes the bare ``name``.
    """
    src = (ROOT / "static" / "boot.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "applyBotName")
    # Every prefix site must come after a no-session guard opening.
    assert re.search(r"if\s*\(\s*!\s*S\.session\s*\)\s*\{", body), (
        "applyBotName must open an explicit !S.session block that owns the "
        "document.title write (and the instance-label prefix) so the prefix "
        "cannot be applied to a title syncTopbar() already composed"
    )

    bare = body.index("document.title=name;")
    prefix = body.index("_instanceLabel+' \\u2022 '+document.title")
    assert bare < prefix, (
        "the bare title must be written before it is prefixed"
    )
    # The guard that opens the block containing the bare write must precede
    # the prefix, and the prefix must sit inside that same brace block.
    guard = body.index("if(!S.session){")
    assert guard < bare < prefix, (
        "the instance-label prefix must be applied inside the !S.session "
        "branch that wrote the bare name — otherwise it accumulates on a "
        "session title owned by syncTopbar()"
    )
    block = body[guard:prefix]
    assert block.count("{") > block.count("}") or block.endswith("{")


def _run_node_apply_bot_name(calls: int, label: str, session: bool) -> dict:
    """Drive the REAL applyBotName() from static/boot.js in a Node VM."""
    driver = r"""
const fs = require('fs');
const boot = fs.readFileSync(process.argv[1], 'utf8');

function extractFunc(name) {
  let i = boot.indexOf('function ' + name + '(');
  if (i < 0) throw new Error(name + ' not found');
  let depth = 0, started = false;
  let start = i;
  for (let j = i; j < boot.length; j++) {
    if (boot[j] === '{') { if (!started) { start = j + 1; started = true; } depth++; }
    else if (boot[j] === '}') { depth--; if (depth === 0) return boot.slice(start, j); }
  }
  throw new Error(name + ' did not terminate');
}

let _title = '';
let _titleWrites = 0;
const document = {
  get title() { return _title; },
  set title(v) { _title = v; _titleWrites++; },
  querySelector() { return null; },
};
const window = { _instanceLabel: process.argv[2] };
let S = { session: process.argv[3] === 'with-session' ? { title: 'Chat' } : null };
const $ = () => null;
function assistantDisplayName() { return 'Hermes'; }
function _applyBusyComposerPlaceholder() {}

const _applyBotName = new Function(
  'document', 'window', 'S', '$', 'assistantDisplayName', '_applyBusyComposerPlaceholder',
  extractFunc('applyBotName')
);
const titles = [];
// S is captured by reference through the closure, so mutating it per call
// mirrors a live profile/session switch the way the browser would.
// document.title is a LIVE property, so the value is snapshotted AFTER the
// call returns while a write flag records whether the call touched it at
// all: a caller that composes the title in two steps (bare write then prefix)
// leaves exactly one final value per call -- never both intermediate writes
// -- and a branch that never writes the title (a session is open, so
// syncTopbar owns it) leaves NO entry at all.
for (let n = 0; n < Number(process.argv[4]); n++) {
  const _before = _titleWrites;
  _applyBotName(document, window, S, $, assistantDisplayName, _applyBusyComposerPlaceholder);
  // Snapshot the LIVE property value only when this call actually wrote
  // it: a caller that composes the title in two steps leaves exactly one
  // final value per call, and a branch that never writes the title (a
  // session is open, so syncTopbar owns it) leaves NO entry at all.
  if (_titleWrites > _before) titles.push(_title);
}
console.log(JSON.stringify(titles));

"""
    if NODE is None:  # pragma: no cover - guarded by skipif markers
        raise RuntimeError("node not on PATH")
    r = subprocess.run(
        [NODE, "-e", driver, str(ROOT / "static" / "boot.js"), label,
         "with-session" if session else "no-session", str(calls)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_bot_name_is_idempotent_for_instance_label():
    """Re-gate finding 3, behavioral: two real applyBotName() calls must
    not stack the prefix. The reviewer reproduced
    ``Production • Production • Production • Chat — Hermes`` from a
    version that prefix-fed an existing title."""
    titles = _run_node_apply_bot_name(calls=2, label="Production", session=False)
    assert titles == ["Production • Hermes", "Production • Hermes"], (
        "applyBotName must rebuild the bare title before prefixing it, so "
        f"repeated calls are idempotent; got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_bot_name_does_not_accumulate_after_three_calls():
    """Same contract, wider: three calls still yield exactly one prefix."""
    titles = _run_node_apply_bot_name(calls=3, label="Production", session=False)
    assert all(t == "Production • Hermes" for t in titles), (
        f"the prefix must never accumulate; got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_bot_name_leaves_active_session_title_alone():
    """#4086 + #7611: with a session open, applyBotName() must not touch
    document.title at all — syncTopbar() owns it, and it applies the label
    prefix itself. A record of no title writes proves both."""
    titles = _run_node_apply_bot_name(calls=2, label="Production", session=True)
    assert titles == [], (
        "applyBotName must not write document.title while a session is "
        f"active (the prefix would accumulate on syncTopbar's title); got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_apply_bot_name_empty_label_keeps_bare_title():
    """Empty label → the default title is byte-identical to pre-PR output."""
    titles = _run_node_apply_bot_name(calls=2, label="", session=False)
    assert titles == ["Hermes", "Hermes"], (
        f"an unset label must leave the title untouched; got {titles}"
    )


def test_sync_topbar_prefixes_with_instance_label():
    """``syncTopbar`` must consume the same label for both the
    no-session and with-session title paths so the tab title
    stays distinguishable while a session is open."""
    src = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "syncTopbar")
    assert "window._instanceLabel" in body, (
        "syncTopbar must consult window._instanceLabel"
    )
    # Both title assignments (no-session and with-session) must
    # apply the same in-place prefix so the empty and non-empty
    # label branches stay consistent.
    assert body.count("_instanceLabel+' \\u2022 '+document.title") >= 2 or (
        body.count("_instanceLabel+' • '+document.title") >= 2
    ), (
        "syncTopbar must apply the instance-label prefix to every "
        "document.title assignment; found fewer than two prefix sites"
    )


def test_instance_label_is_not_writable_from_post_settings(monkeypatch):
    """The label is installation-scoped and must not be
    editable from the WebUI settings UI. The POST handler at
    ``/api/settings`` accepts ``bot_name`` and other user-tunable
    fields, but the ``instance_label`` field must not be
    consumed from the request body. A user submitting
    ``{instance_label: 'CustomLabel'}`` must not have it
    applied; only the env var and config.yaml control the
    value."""
    src = (ROOT / "api" / "routes.py").read_text(encoding="utf-8")
    # The GET path reads the label from the helper; the POST
    # path must not interpret it. The simplest check: the
    # docstring for bot_name handling explicitly mentions the
    # field name, but instance_label must not appear in any
    # 'body[...]' reads inside the POST handler.
    # Concretely: find the POST /api/settings block and ensure
    # it does not branch on "instance_label" in body.
    assert '"instance_label" in body' not in src, (
        "instance_label must not be a writable body field on "
        "/api/settings POST — the value is installation-scoped"
    )
    # And the GET path must surface it from the helper.
    assert "settings[\"instance_label\"] = _read_instance_label()" in src, (
        "/api/settings GET must surface instance_label so the "
        "frontend can prefix the title"
    )


# ── helpers ─────────────────────────────────────────────────────────────────

# ── re-gate finding 4: non-chat panel titles keep the label ──────────────────


def test_sync_app_titlebar_prefixes_panel_titles_with_instance_label():
    """Re-gate finding 4, static: ``syncAppTitlebar`` must prefix the
    instance label onto the title it builds for non-chat panels.

    The reviewer's reproduction: on the chat panel the label shows up
    (``syncTopbar`` owns the title there), but the moment the user
    switches to Settings / Tasks / Kanban the tab title reverts to a
    bare ``Settings -- Hermes``. ``syncAppTitlebar`` REBUILDS
    ``document.title`` from the panel name instead of appending to an
    existing title, so a label that lived only in the boot paths was
    silently discarded exactly while the user is configuring a
    multi-instance install.

    Locked shape: the prefix statement must sit inside the same
    non-chat block that (re)builds ``document.title``, so the label is
    applied to a title this function owns and the chat branch -- which
    never reaches it -- cannot accumulate prefixes.
    """
    src = (ROOT / "static" / "panels.js").read_text(encoding="utf-8")
    body = _extract_function_body(src, "syncAppTitlebar")
    assert "window._instanceLabel" in body, (
        "syncAppTitlebar must consult window._instanceLabel"
    )
    guard = body.index("if (panel !== 'chat') {")
    bare = body.index("document.title = bot ? mainText + ' \\u2014 ' + bot : mainText;")
    prefix = body.index("_instanceLabel+' \\u2022 '+document.title")
    assert guard < bare < prefix, (
        "the instance-label prefix must be applied inside the "
        "non-chat branch, AFTER that branch rebuilt document.title from "
        "the panel name -- otherwise the label is dropped on every "
        "non-chat panel"
    )
    block = body[guard:prefix]
    assert block.count("{") > block.count("}") or block.endswith("{")
    # Only one prefix site: the chat panel never reaches the block, so a
    # second prefix site could only mean prefixing a title the function
    # did not just rebuild -- which is how finding 3's accumulation began.
    assert body.count("_instanceLabel+' \\u2022 '+document.title") == 1, (
        "syncAppTitlebar must apply the prefix at exactly one site, "
        "inside the branch that owns the title rebuild"
    )


def _run_node_sync_app_titlebar(
    panel: str, label: str, session_title: str = "", calls: int = 1
) -> dict:
    """Drive the REAL syncAppTitlebar() from static/panels.js in a Node VM."""
    driver = r"""
const fs = require('fs');
const panels = fs.readFileSync(process.argv[1], 'utf8');

function extractFunc(name) {
  let i = panels.indexOf('function ' + name + '(');
  if (i < 0) throw new Error(name + ' not found');
  let depth = 0, started = false;
  let start = i;
  for (let j = i; j < panels.length; j++) {
    if (panels[j] === '{') { if (!started) { start = j + 1; started = true; } depth++; }
    else if (panels[j] === '}') { depth--; if (depth === 0) return panels.slice(start, j); }
  }
  throw new Error(name + ' did not terminate');
}

// Copy the module-level APP_TITLEBAR_KEYS map VERBATIM so the extracted
// body resolves the real panel->i18n-key table rather than a test copy.
function extractConst(name) {
  const marker = 'const ' + name + ' = ';
  let i = panels.indexOf(marker);
  if (i < 0) throw new Error(name + ' not found');
  let depth = 0;
  for (let j = i + marker.length; j < panels.length; j++) {
    const ch = panels[j];
    if (ch === '{' || ch === '[' || ch === '(') depth++;
    else if (ch === '}' || ch === ']' || ch === ')') depth--;
    else if (ch === ';' && depth === 0) return panels.slice(i, j + 1);
  }
  throw new Error(name + ' did not terminate');
}

function fakeEl() {
  return {
    textContent: '', hidden: false, value: '', className: '', type: '',
    style: {}, onclick: null, onblur: null, onkeydown: null,
    classList: { add() {}, remove() {}, contains() { return false; } },
    addEventListener() {}, removeEventListener() {}, setAttribute() {},
    appendChild() {}, replaceWith() {}, remove() {}, focus() {}, select() {},
    contains() { return false; },
    getBoundingClientRect() { return { top: 0, bottom: 0, left: 0, right: 0 }; },
  };
}

let _title = '';
let _titleWrites = 0;
const document = {
  get title() { return _title; },
  set title(v) { _title = v; _titleWrites++; },
  // appTitlebarSub is deliberately absent so the mocked surface stays
  // minimal; the title assertions do not depend on it.
  getElementById(id) { return id === 'appTitlebarTitle' ? fakeEl() : null; },
  querySelector() { return null; },
  createElement() { return fakeEl(); },
  createTextNode(t) { return { text: t }; },
  removeEventListener() {}, addEventListener() {},
  body: { appendChild() {} },
};
const window = { _instanceLabel: process.argv[2], innerWidth: 1280 };
const S = { session: process.argv[4] ? { title: process.argv[4] } : null,
            messages: [] };
const _currentPanel = process.argv[5];
const _renamingAppTitlebar = false;
const t = (key) => key;
function assistantDisplayName() { return 'Hermes'; }

const _syncAppTitlebar = new Function(
  'document', 'window', 'S', 't', 'assistantDisplayName',
  '_currentPanel', '_renamingAppTitlebar',
  // Inline the const (not a parameter name -- that would be a
  // redeclaration SyntaxError) and wrap the extracted body so it
  // returns nothing, exactly like the real function.
  extractConst('APP_TITLEBAR_KEYS') + '\n' +
  extractFunc('syncAppTitlebar')
);
const titles = [];
for (let n = 0; n < Number(process.argv[3]); n++) {
  const _before = _titleWrites;
  _syncAppTitlebar(document, window, S, t, assistantDisplayName,
                   _currentPanel, _renamingAppTitlebar);
  // Snapshot the LIVE property value only when this call actually wrote
  // it: a caller that composes the title in two steps (bare rebuild then
  // prefix) leaves exactly one final value per call, never both
  // intermediate writes; a branch that never writes the title (chat
  // panel: syncTopbar owns it) leaves NO entry at all.
  if (_titleWrites > _before) titles.push(_title);
}
console.log(JSON.stringify(titles));

"""
    if NODE is None:  # pragma: no cover - guarded by skipif markers
        raise RuntimeError("node not on PATH")
    r = subprocess.run(
        [NODE, "-e", driver, str(ROOT / "static" / "panels.js"), label,
         str(calls), session_title, panel],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError(f"node failed: {r.stderr}")
    return json.loads(r.stdout.strip().splitlines()[-1])


# The non-chat panels the sidebar can land on, including ones with no
# APP_TITLEBAR_KEYS entry (they fall back to the capitalised name).
_ALL_NON_CHAT_PANELS = sorted(
    {"settings", "skills", "memory", "tasks", "kanban", "workspaces",
     "profiles", "insights", "logs", "todos", "plugin"}
)


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
@pytest.mark.parametrize("panel", _ALL_NON_CHAT_PANELS)
def test_panel_title_keeps_instance_label_prefix(panel):
    """Re-gate finding 4, behavioral: every non-chat panel must carry the
    label in the tab title. Walking the real panel list means a panel later
    added to the sidebar without the prefix fails here."""
    titles = _run_node_sync_app_titlebar(panel, "Production")
    assert titles, f"panel {panel} must write document.title"
    assert all(t.startswith("Production \u2022 ") for t in titles), (
        f"panel '{panel}' lost the instance-label prefix; got {titles}"
    )
    # Exactly one prefix, and the panel name still follows it.
    assert titles[0].count("Production \u2022 ") == 1


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_title_prefix_is_idempotent():
    """Three consecutive syncAppTitlebar() calls on one panel must not stack
    the prefix: the function rebuilds the title from the panel name before
    prefixing it, so later calls are no-ops."""
    titles = _run_node_sync_app_titlebar("settings", "Production", calls=3)
    assert len(titles) == 3 and len(set(titles)) == 1, (
        f"the panel prefix must never accumulate; got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_title_without_instance_label_is_bare():
    """Empty label: the panel title is byte-identical to the pre-PR output."""
    titles = _run_node_sync_app_titlebar("settings", "")
    assert titles == ["tab_settings \u2014 Hermes"], (
        f"an unset label must leave the panel title untouched; got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_panel_title_prefix_does_not_clobber_session_title():
    """A panel switch after a session was open must not stack the prefix on
    syncTopbar()'s "<session> -- <assistant>" title: the non-chat branch
    rebuilds the title from the panel name first, so the session half is
    replaced, never accumulated."""
    titles = _run_node_sync_app_titlebar(
        "settings", "Production", session_title="My chat"
    )
    assert titles == ["Production \u2022 tab_settings \u2014 Hermes"], (
        "switching panels must replace, not stack on, the session title; "
        f"got {titles}"
    )


@pytest.mark.skipif(NODE is None, reason="node not on PATH")
def test_chat_panel_title_is_owned_by_sync_topbar():
    """The chat panel must NOT touch document.title at all: syncTopbar() is
    the sole owner of "<session> -- <assistant>" and applies the label
    itself. A title write here would double-prefix the session title."""
    titles = _run_node_sync_app_titlebar(
        "chat", "Production", session_title="My chat", calls=2
    )
    assert titles == [], (
        "syncAppTitlebar must not write document.title on the chat panel "
        f"(syncTopbar owns it and applies the label itself); got {titles}"
    )



def _extract_function_body(src: str, name: str) -> str:
    """Inline copy of the same helper used in the issue #7352
    test file — keeps this file self-contained and avoids
    pulling in a private test-utility module."""
    marker = f"function {name}("
    start = src.find(marker)
    assert start != -1, f"{name} not found"
    paren = src.find("(", start)
    assert paren != -1
    depth = 0
    for idx in range(paren, len(src)):
        ch = src[idx]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                brace = src.find("{", idx)
                break
    else:
        raise AssertionError(f"{name} params did not terminate")
    assert brace != -1
    depth = 0
    for idx in range(brace, len(src)):
        ch = src[idx]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[brace + 1 : idx]
    raise AssertionError(f"{name} body did not terminate")


def test_a_profile_dotenv_cannot_repoint_installation_configuration(tmp_path, monkeypatch):
    """A profile must not be able to hijack installation-scoped config (#7611).

    ``_installation_config_path`` resolves the installation-scoped config, and
    the instance label is read from whatever it resolves to. A profile whose
    ``.env`` sets ``HERMES_CONFIG_PATH`` to its own config.yaml would
    repoint installation configuration at a profile file -- and because
    ``_read_installation_config`` reads ``instance_name`` from whatever that
    path resolves to, the installation label could change per profile. That is
    exactly the profile-scoping the installation-scoped label exists to avoid.

    #7655 round 7: the guarantee is enforced at the READ site, not by
    protecting the key globally. Upstream #8432 round 4 (76986aef0) lets a
    profile's ``.env`` SET ``HERMES_CONFIG_PATH`` and tells a profile-chosen
    path from an operator-chosen one by the loader bookkeeping, so protecting
    the key globally broke that shared contract (and upstream's own
    ``test_8432_a_config_path_a_profile_env_chose_is_not_shared`` went red).
    The resolver now reads the INSTALLATION SNAPSHOT -- the operator env
    captured at the startup boundary, before any profile writer ran.
    """
    from api import profiles as prof

    assert "HERMES_CONFIG_PATH" not in prof._PROTECTED_ENV_KEYS, (
        "HERMES_CONFIG_PATH must not be globally protected: upstream #8432 "
        "round 4 lets a profile .env set it"
    )

    routes = _load_routes()
    operator_cfg = tmp_path / "installation.yaml"
    operator_cfg.write_text("instance_name: real-deployment\n", encoding="utf-8")
    sneaky_cfg = tmp_path / "sneaky-config.yaml"
    sneaky_cfg.write_text("instance_name: hijacked\n", encoding="utf-8")

    # The operator's env at the startup boundary, then the profile's attempt.
    prev_loaded = prof._loaded_profile_env_keys
    prev_snapshot = dict(getattr(prof, "_INSTALLATION_ENV_SNAPSHOT", {}) or {})
    monkeypatch.setattr(prof, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(prof, "_INSTALLATION_ENV_SNAPSHOT", {})
    try:
        monkeypatch.setenv("HERMES_CONFIG_PATH", str(operator_cfg))
        prof.capture_installation_env_snapshot()

        profile_home = tmp_path / "profiles" / "sneaky"
        profile_home.mkdir(parents=True)
        (profile_home / ".env").write_text(
            f"HERMES_CONFIG_PATH={sneaky_cfg}\nSOME_OTHER_KEY=x\n", encoding="utf-8"
        )
        prof._reload_dotenv(profile_home)

        # The loader still projects the key (upstream contract) ...
        assert os.environ.get("HERMES_CONFIG_PATH") == str(sneaky_cfg)
        assert "HERMES_CONFIG_PATH" in (prof._loaded_profile_env_keys or set())
        # ... but the installation resolver follows the OPERATOR's path.
        assert Path(routes._installation_config_path()) == operator_cfg
        # And the label resolver therefore cannot be steered by the profile.
        assert routes._read_instance_label() == "real-deployment"
    finally:
        prof._loaded_profile_env_keys = prev_loaded
        prof._INSTALLATION_ENV_SNAPSHOT = prev_snapshot

# ── #7655: base-config placeholders resolve against operator env only ───────


def test_base_config_placeholder_cannot_be_claimed_by_profile_dotenv(installation_env, monkeypatch, tmp_path):
    """#7655: an installation-scoped ``${VAR}`` placeholder must not resolve
    against a profile's ``.env``.

    The operator never set ``SLOT_NAME``, Alice's profile .env sets it, and the
    installation config expands ``instance_name: ${SLOT_NAME}``. The label must
    stay the literal ``${SLOT_NAME}`` -- an operator sees their own file, not
    another deployment's slot name in the tab title.

    Driven through the real startup boundary (``installation_env.boot``) so the
    snapshot genuinely predates the profile projection, exactly as it does in a
    real process.
    """
    import api.profiles as prof
    import api.routes as routes

    alice = installation_env.profile("alice")
    (alice / ".env").write_text(
        "SLOT_NAME=AliceProfile\nUNRELATED_PROFILE_KEY=x\n", encoding="utf-8"
    )
    _install_config(monkeypatch, tmp_path, {"instance_name": "${SLOT_NAME}"})
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(prof, "_DEFAULT_HERMES_HOME", installation_env.base)

    # The boundary boots with no operator SLOT_NAME, then the profile loads.
    installation_env.boot(operator_env={})
    prof._reload_dotenv(alice)
    # The profile's value IS in process env -- that is the leak under test.
    assert os.environ.get("SLOT_NAME") == "AliceProfile"
    assert "SLOT_NAME" in (prof._loaded_profile_env_keys or set())

    label = routes._read_instance_label()
    assert label != "AliceProfile", (
        "a profile .env claimed the installation-scoped label placeholder"
    )
    assert label == "${SLOT_NAME}", (
        "an unowned placeholder must fail closed to its literal form, "
        f"got {label!r}"
    )


def test_operator_value_survives_profile_switch_and_reload(installation_env, monkeypatch, tmp_path):
    """#7655: the operator's launch value keeps resolving across switches.

    Alice -> Bob -> a profile with no matching key, with Alice's own .env
    carrying the SAME name as the operator's. Each switch must keep the label
    reading the operator's value, never the profile's, and the operator value
    must not be popped by the loader's cleanup.
    """
    import api.profiles as prof
    import api.routes as routes

    for name, value in (("alice", "AliceProfile"), ("bob", "BobProfile")):
        d = installation_env.profile(name)
        (d / ".env").write_text(f"SLOT_NAME={value}\n", encoding="utf-8")
    # A profile with no matching key: the loader pops the session's key.
    installation_env.profile("empty")

    _install_config(monkeypatch, tmp_path, {"instance_name": "${SLOT_NAME}"})
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(prof, "_DEFAULT_HERMES_HOME", installation_env.base)

    # The launcher set the real value BEFORE any profile ran.
    installation_env.boot(operator_env={"SLOT_NAME": "Production"})

    for name in ("alice", "bob", "empty"):
        prof._reload_dotenv(installation_env.profile(name))
        # For alice/bob the profile's value IS in process env (the loader's own
        # projection); for ``empty`` the loader pops it, leaving None. Either
        # way the LABEL must read the operator's launch value.
        label = routes._read_instance_label()
        assert label == "Production", (
            f"after switching to {name} the label must still be the operator's, "
            f"got {label!r}"
        )

    # The operator's own process env is never adopted or destroyed.
    assert os.environ.get("SLOT_NAME") in {"Production", None}


def test_operator_and_profile_equal_value_still_resolves(installation_env, monkeypatch, tmp_path):
    """The equal-value case: operator and profile agree.

    The old loader only recorded a preimage when the value DIFFERED, so with
    identical values nothing was tracked and the label fell back to the literal
    ``${SLOT_NAME}`` -- a visible regression for an operator who had done
    nothing wrong.
    """
    import api.profiles as prof
    import api.routes as routes

    alice = installation_env.profile("alice")
    (alice / ".env").write_text("SLOT_NAME=Production\n", encoding="utf-8")
    _install_config(monkeypatch, tmp_path, {"instance_name": "${SLOT_NAME}"})
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(prof, "_DEFAULT_HERMES_HOME", installation_env.base)

    installation_env.boot(operator_env={"SLOT_NAME": "Production"})
    prof._reload_dotenv(alice)
    assert os.environ.get("SLOT_NAME") == "Production"

    assert routes._read_instance_label() == "Production", (
        "an operator value that a profile happens to agree with must still "
        "resolve, not fall back to the literal placeholder"
    )


def test_duplicate_dotenv_lines_do_not_steal_the_operator_value(installation_env, monkeypatch, tmp_path):
    """Duplicate keys in one profile .env, with and without an operator value.

    The old loader recorded ``_prior`` immediately before EACH line, so the
    second ``SLOT_NAME=`` line stored the FIRST profile line's value as the
    "operator" preimage -- the label then resolved to a profile-owned value.
    """
    import api.profiles as prof
    import api.routes as routes

    alice = installation_env.profile("alice")
    (alice / ".env").write_text(
        "SLOT_NAME=AliceFirst\nDUP_SLOT=AliceFirst\nSLOT_NAME=AliceSecond\n",
        encoding="utf-8",
    )
    _install_config(
        monkeypatch, tmp_path, {"instance_name": "${SLOT_NAME}", "webui": {"instance_name": "${DUP_SLOT}"}}
    )
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(prof, "_DEFAULT_HERMES_HOME", installation_env.base)

    # -- without an operator value: both stay literal ----------------------
    installation_env.boot(operator_env={})
    prof._reload_dotenv(alice)
    assert os.environ.get("SLOT_NAME") == "AliceSecond"  # last line wins
    assert routes._read_instance_label() == "${SLOT_NAME}", (
        "a duplicate-line profile .env must not manufacture an operator value"
    )


def test_root_settings_read_while_background_scope_is_active(installation_env, monkeypatch, tmp_path):
    """A root settings read while a named-profile background scope is live.

    ``profile_env_for_background_worker`` installs the profile's runtime env
    into ``os.environ`` WITHOUT touching the loader's bookkeeping, so a live-env
    read returned the profile's value. This is the ordering the round-6 review
    found: the snapshot is immune because it was taken before any injection.
    """
    import api.profiles as prof
    import api.routes as routes

    alice = installation_env.profile("alice")
    (alice / ".env").write_text("SLOT_NAME=AliceProfile\n", encoding="utf-8")
    _install_config(monkeypatch, tmp_path, {"instance_name": "${SLOT_NAME}"})
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(prof, "_DEFAULT_HERMES_HOME", installation_env.base)

    installation_env.boot(operator_env={})
    prof._reload_dotenv(alice)

    # A background worker for Alice installs her runtime env with none of the
    # loader's bookkeeping involved -- the exact writer that leaked before.
    monkeypatch.setenv("SLOT_NAME", "AliceProfile")

    label = routes._read_instance_label()
    assert label != "AliceProfile", (
        "a background worker's injected profile value reached the "
        "installation-scoped label"
    )
    assert label == "${SLOT_NAME}"


def test_duplicate_dotenv_lines_with_operator_value(installation_env, monkeypatch, tmp_path):
    """The duplicate-line case WITH an operator value on both names.

    The old loader recorded ``_prior`` immediately before EACH line, so the
    second ``SLOT_NAME=`` line stored the FIRST profile line's value as the
    "operator" preimage -- the label then resolved to a profile-owned value.
    With an operator value present, that line overwrote the saved operator
    value instead.

    The label must resolve to the operator's value for BOTH names.
    """
    import api.profiles as prof
    import api.routes as routes

    alice = installation_env.profile("alice")
    (alice / ".env").write_text(
        "SLOT_NAME=AliceFirst\nDUP_SLOT=AliceFirst\nSLOT_NAME=AliceSecond\n",
        encoding="utf-8",
    )
    _install_config(
        monkeypatch,
        tmp_path,
        {
            "instance_name": "${SLOT_NAME}",
            "webui": {"instance_name": "${DUP_SLOT}"},
        },
    )
    monkeypatch.delenv("HERMES_WEBUI_INSTANCE_NAME", raising=False)
    monkeypatch.setattr(prof, "_DEFAULT_HERMES_HOME", installation_env.base)

    # The launcher set both names before any profile ran.
    installation_env.boot(operator_env={"SLOT_NAME": "Production", "DUP_SLOT": "Prod-B"})
    prof._reload_dotenv(alice)
    assert os.environ.get("SLOT_NAME") == "AliceSecond"

    cfg = routes._read_installation_config()
    assert routes._read_instance_label() == "Production", (
        f"the operator's SLOT_NAME must win over the duplicate profile lines: {cfg!r}"
    )
    assert (cfg.get("webui") or {}).get("instance_name") == "Prod-B", (
        f"the operator's DUP_SLOT must win over the duplicate profile lines: {cfg!r}"
    )


# ── Fixture process-state isolation (greptile P2, 2026-10-05) ────────────────


def test_process_home_state_is_fully_restored_after_the_startup_boundary(
    tmp_path
):
    """The startup boundary's process state must be fully restorable.

    ``_set_hermes_home()`` — the real call the fixture's ``boot()`` makes —
    mutates the HERMES_HOME env var, the process-profile home pin, the
    imported skill modules' cached paths, the cron modules' cached paths and
    the Agent's process-home pin. All of them are process-global and point at
    a ``tmp_path`` home that is deleted with the test, so a teardown that
    leaves any of them behind makes a later test run against a deleted home.

    This drives the REAL ``_set_hermes_home()`` and then the real restore
    helper, and asserts every mutated piece of process state is back where it
    started. It is the observable form of the finding: if the restore stops
    covering one of them, this test fails.

    The skill/cron module assertions are conditional on the module being
    importable: the Agent package is not installed in every environment, and
    a module that was never imported has no path to leak.
    """
    import api.profiles as prof

    snapshot = _snapshot_process_home_state(prof)
    before = {
        "hermes_home": os.environ.get("HERMES_HOME"),
        "had_hermes_home": "HERMES_HOME" in os.environ,
        "process_profile_home": getattr(prof, "_PROCESS_PROFILE_HOME", None),
    }

    # The real mutation.
    prof._set_hermes_home(tmp_path / "temporary-home")

    assert os.environ.get("HERMES_HOME") == str(tmp_path / "temporary-home"), (
        "the real _set_hermes_home() did not move HERMES_HOME — the case "
        "below would not be exercising the leak"
    )
    assert getattr(prof, "_PROCESS_PROFILE_HOME", None) == str(tmp_path / "temporary-home"), (
        "the real _set_hermes_home() did not move the process-profile home pin "
        "— the case below would not be exercising the leak"
    )

    # The real restore.
    _restore_process_home_state(prof, snapshot)

    assert os.environ.get("HERMES_HOME") == before["hermes_home"], (
        f"HERMES_HOME was not restored: {before['hermes_home']!r} -> "
        f"{os.environ.get('HERMES_HOME')!r}"
    )
    assert ("HERMES_HOME" in os.environ) == before["had_hermes_home"], (
        "the PRESENCE of HERMES_HOME was not restored — a home that did not "
        "exist before must not exist after"
    )
    assert getattr(prof, "_PROCESS_PROFILE_HOME", None) == before["process_profile_home"], (
        "the process-profile home pin was not restored: "
        f"{before['process_profile_home']!r} -> "
        f"{getattr(prof, '_PROCESS_PROFILE_HOME', None)!r}"
    )

    # The cron modules' cached paths are back to their pre-call values.
    for (module_name, attr), (_module, _attr, _had, value) in snapshot["cron_home"].items():
        try:
            module = __import__(module_name, fromlist=["_"])
        except Exception:
            continue
        assert getattr(module, attr, None) == value, (
            f"{module_name}.{attr} was not restored: {value!r} -> "
            f"{getattr(module, attr, None)!r}"
        )

    # The skill modules' cached paths are back to their pre-call values.
    # A module that was never imported cannot leak, so the assertion is
    # skipped for it — but a FAKE module injected here exercises the restore
    # path even where the Agent package is absent, so the skill restore is
    # never vacuously green.
    for module_name, values in snapshot["skill_home"].items():
        module = sys.modules.get(module_name)
        if not values.get("module_present"):
            assert module is None, (
                f"{module_name} was not importable before the boundary and is "
                "importable after — the restore did not put it back"
            )
            continue
        if module is None:
            continue
        for attr in ("HERMES_HOME", "SKILLS_DIR"):
            if values.get(f"has_{attr}"):
                assert getattr(module, attr, None) == values.get(attr), (
                    f"{module_name}.{attr} was not restored: "
                    f"{values.get(attr)!r} -> {getattr(module, attr, None)!r}"
                )


def test_skill_module_restore_is_not_vacuous(tmp_path, monkeypatch):
    """The skill-module restore must work even where the Agent is absent.

    ``patch_skill_home_modules()`` only touches modules that are already in
    ``sys.modules``, so a package that was never imported has nothing to
    leak — but that also makes the skill restore silently untested in
    environments without the Agent package. This injects a fake skill module
    with the real attribute names, runs the real boundary against it, and
    asserts the restore puts its cached paths back.
    """
    import api.profiles as prof

    fake = types.ModuleType("tools.skills_tool")
    fake.HERMES_HOME = Path("/original/skill/home")
    fake.SKILLS_DIR = Path("/original/skill/home/skills")
    monkeypatch.setitem(sys.modules, "tools.skills_tool", fake)

    snapshot = _snapshot_process_home_state(prof)
    assert snapshot["skill_home"]["tools.skills_tool"]["module_present"] is True, (
        "the fake skill module was not captured by the snapshot — the case "
        "below would not be exercising the restore"
    )

    # The real mutation: the boundary repoints the module at the new home.
    prof._set_hermes_home(tmp_path / "temporary-home")
    assert fake.HERMES_HOME == tmp_path / "temporary-home", (
        "the real _set_hermes_home() did not repoint the skill module — the "
        "case below would not be exercising the restore"
    )

    # The real restore.
    _restore_process_home_state(prof, snapshot)

    assert fake.HERMES_HOME == Path("/original/skill/home"), (
        "the skill module's HERMES_HOME was not restored: "
        f"{fake.HERMES_HOME!r}"
    )
    assert fake.SKILLS_DIR == Path("/original/skill/home/skills"), (
        f"the skill module's SKILLS_DIR was not restored: {fake.SKILLS_DIR!r}"
    )
