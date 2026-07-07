"""Regression test for #5567 — cross-profile HERMES_HOME race at the config reader.

Root cause: `profile_env_for_background_worker` mirrors the profile's HERMES_HOME
into the process-global `os.environ`, and the worker body runs outside the setup
lock. A concurrent cross-profile worker can clobber `os.environ["HERMES_HOME"]`
mid-body, so the agent config reader (`hermes_cli.config.get_config_path` /
`load_config`, which read `get_hermes_home()`) resolves the WRONG profile's
config — intermittent turn-init failures referencing another profile's provider.

Fix (#5567): when hermes-agent >= v0.18.0 exposes the context-local home
override (`hermes_constants.set_hermes_home_override`), the worker scope installs
it so `get_hermes_home()` resolves THIS task's profile home from task-local state,
immune to the process-global clobber — without serializing workers.

Per #2321's acceptance criteria, this exercises the REAL
`hermes_cli.config.load_config()` against a non-default profile with an
intentional mid-body `os.environ` clobber and NO mocking of the production reader.

Degrades gracefully on agents without the override (skips with a clear reason).
"""
import os
import textwrap
from pathlib import Path

import pytest

# The production reader — imported unmocked, exactly as #2321 requires. Skip the
# whole module if the agent isn't importable in this environment.
config_mod = pytest.importorskip("hermes_cli.config")
hermes_constants = pytest.importorskip("hermes_constants")

HAS_OVERRIDE = hasattr(hermes_constants, "set_hermes_home_override") and hasattr(
    hermes_constants, "get_hermes_home"
)

from api import profiles as profiles_api  # noqa: E402


def _seed_profile_home(base: Path, name: str, provider: str, model: str) -> Path:
    home = base / name
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        textwrap.dedent(
            f"""\
            model:
              default: {model}
            provider: {provider}
            """
        ),
        encoding="utf-8",
    )
    return home


@pytest.mark.skipif(
    not HAS_OVERRIDE,
    reason="hermes-agent < v0.18.0: no set_hermes_home_override; WebUI degrades to the os.environ mirror",
)
def test_load_config_resolves_worker_profile_despite_env_clobber(tmp_path, monkeypatch):
    """The crux (#2321 criterion): inside profile_env_for_background_worker(A),
    a concurrent clobber of os.environ['HERMES_HOME']=B must NOT make the real
    load_config() read B — the context-local override pins A."""
    home_a = _seed_profile_home(tmp_path, "alpha", provider="anthropic", model="claude-x")
    home_b = _seed_profile_home(tmp_path, "beta", provider="ollama", model="llama-y")

    # The CM's INPUT (which profile home to scope to) — this is not the reader
    # under test; the reader is the real hermes_cli.config below.
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: home_a)

    # Establish a benign starting env, then simulate the race: while the worker
    # body for profile A runs, a sibling profile-B worker clobbers the global.
    monkeypatch.setenv("HERMES_HOME", str(home_a))

    # Clear any cached config so load_config actually hits the resolver.
    for fn in ("reload_config", "_reset_config_cache", "clear_config_cache"):
        if hasattr(config_mod, fn):
            try:
                getattr(config_mod, fn)()
            except Exception:
                pass

    with profiles_api.profile_env_for_background_worker("alpha", "test worker"):
        # The clobber: another profile's worker overwrites the process global.
        os.environ["HERMES_HOME"] = str(home_b)
        # get_config_path must resolve profile A via the context-local override,
        # NOT profile B from the clobbered os.environ.
        resolved = config_mod.get_config_path()
        assert resolved == home_a / "config.yaml", (
            f"config path must resolve profile A ({home_a}) via the context-local "
            f"override despite os.environ clobbered to B ({home_b}); got {resolved}"
        )
        # And the real load_config() must read A's model, not B's.
        cfg = config_mod.load_config()
        model_default = (cfg.get("model") or {}).get("default")
        assert model_default == "claude-x", (
            f"load_config must read profile A's model 'claude-x' despite the "
            f"HERMES_HOME clobber to B; got {model_default!r} (B is 'llama-y')"
        )


@pytest.mark.skipif(
    not HAS_OVERRIDE,
    reason="requires the v0.18.0 override to assert the override is cleared on exit",
)
def test_override_is_cleared_after_worker_exits(tmp_path, monkeypatch):
    """The context-local override must not leak past the worker scope."""
    home_a = _seed_profile_home(tmp_path, "alpha", provider="anthropic", model="claude-x")
    monkeypatch.setattr(profiles_api, "get_hermes_home_for_profile", lambda name: home_a)

    assert hermes_constants.get_hermes_home_override() is None
    with profiles_api.profile_env_for_background_worker("alpha", "test worker"):
        assert hermes_constants.get_hermes_home_override() == str(home_a)
    # Cleared on exit — no leak into subsequent tasks on this context.
    assert hermes_constants.get_hermes_home_override() is None


def test_graceful_degradation_resolver_is_optional():
    """On an agent WITHOUT the override, the resolver returns None and the CM
    falls back to the pre-existing os.environ mirror — never raises. We assert
    the resolver is import-safe and boolean-clean regardless of agent version."""
    mod = profiles_api._resolve_hermes_home_override()
    if HAS_OVERRIDE:
        assert mod is not None and hasattr(mod, "set_hermes_home_override")
    else:
        assert mod is None  # older agent: graceful no-op, os.environ mirror stays
