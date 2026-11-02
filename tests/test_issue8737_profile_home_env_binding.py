"""``get_config_for_profile_home()`` resolves ``${VAR}`` under the OWNING profile's
``.env`` — never the calling thread's / process env (#8737).

#8129 keeps ``${VAR}`` references on disk and defines ONE rule for reading them:
expand a profile's RAW config document under that profile's env binding
(``api/profiles.expand_profile_config`` / ``load_profile_config``). The streaming
repair follows it (``api/streaming.py:_apply_profile_home_context_to_streaming_model``),
but ``get_config_for_profile_home`` — the reader every session-scoped worker and
workspace call uses — expanded through ``_load_yaml_config_file`` instead, i.e.
against whatever env the CALLING thread happens to carry. A detached worker has
no thread-local profile env, so the lookup fell through to ``os.environ``: a
reference that only the profile's own ``.env`` defines stayed literal (or
resolved through the wrong profile), and a value one profile mirrored into the
process env surfaced in another profile's config.

The fixture below makes that concrete: two named profiles with DIFFERENT
``.env`` files, the SAME ``${VAR}`` references, and a process env poisoned with
a third value — each profile must resolve its own, and a reference no profile
owns must stay the literal ``${VAR}`` (never an empty string).
"""

import os
from types import MappingProxyType

import pytest
import yaml

CONFIG_TEMPLATE = """\
model:
  provider: ${SHARED_PROVIDER}
  default: ${SHARED_MODEL}
terminal:
  backend: ${SHARED_BACKEND}
agent:
  clarify_timeout: 30
"""


@pytest.fixture
def two_profiles(tmp_path, monkeypatch):
    """Root home with two named profiles whose ``.env`` disagree on one key."""
    import api.paths as paths
    import api.profiles as profiles

    root = tmp_path / ".hermes"
    per_profile = {
        "alpha": {
            "SHARED_PROVIDER": "openrouter",
            "SHARED_MODEL": "model-alpha",
            "SHARED_BACKEND": "alpha-term",
        },
        "beta": {
            "SHARED_PROVIDER": "anthropic",
            "SHARED_MODEL": "model-beta",
            "SHARED_BACKEND": "beta-term",
        },
    }
    homes = {}
    for name, values in per_profile.items():
        home = root / "profiles" / name
        home.mkdir(parents=True)
        (home / ".env").write_text(
            "".join(f"{key}={value}\n" for key, value in values.items()), encoding="utf-8"
        )
        (home / "config.yaml").write_text(CONFIG_TEMPLATE, encoding="utf-8")
        homes[name] = home

    # Ambient resolution pinned to a root home that owns NEITHER config under
    # test, so get_config_for_profile_home must read each profile off disk.
    monkeypatch.setattr(profiles, "_DEFAULT_HERMES_HOME", root)
    monkeypatch.setattr(profiles, "_active_profile", "default")
    monkeypatch.setattr(profiles, "_loaded_profile_env_keys", set())
    monkeypatch.setattr(
        profiles,
        "list_profiles_api",
        lambda: [{"name": "default", "is_default": True}, {"name": "alpha"}, {"name": "beta"}],
    )
    monkeypatch.setattr(paths, "STARTUP_ENV", MappingProxyType({}), raising=False)
    monkeypatch.delenv("HERMES_CONFIG_PATH", raising=False)
    profiles._invalidate_root_profile_cache()
    profiles._tls.profile = None

    # Simulate the state a profile-switching worker leaves behind: its own
    # values mirrored into the server process env. None of these may be read.
    for key, value in {
        "SHARED_PROVIDER": "process-leak",
        "SHARED_MODEL": "process-leak",
        "SHARED_BACKEND": "process-leak",
        "GATE_ABSENT": "process-leak",
    }.items():
        monkeypatch.setenv(key, value)

    yield homes

    profiles._tls.profile = None
    profiles._invalidate_root_profile_cache()


def _expand(config_text, home):
    from api.config import _expand_env_vars
    from api.profiles import expand_profile_config

    return expand_profile_config(home, yaml.safe_load(config_text))


def test_each_profile_resolves_from_its_own_dotenv(two_profiles):
    from api.config import get_config_for_profile_home

    alpha = get_config_for_profile_home(two_profiles["alpha"])
    beta = get_config_for_profile_home(two_profiles["beta"])

    assert alpha["model"]["default"] == "model-alpha"
    assert alpha["model"]["provider"] == "openrouter"
    assert alpha["terminal"]["backend"] == "alpha-term"

    assert beta["model"]["default"] == "model-beta"
    assert beta["model"]["provider"] == "anthropic"
    assert beta["terminal"]["backend"] == "beta-term"


def test_no_cross_profile_or_process_env_leak(two_profiles, monkeypatch):
    """Holding one profile's .env open as a worker must not feed the other."""
    import threading

    import api.profiles as profiles
    from api.config import get_config_for_profile_home

    entered, release, errors = threading.Event(), threading.Event(), []

    def body():
        try:
            with profiles.profile_env_for_background_worker(
                "alpha", scope_skill_modules=False
            ):
                entered.set()
                release.wait(10)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            entered.set()

    worker = threading.Thread(target=body, daemon=True)
    worker.start()
    try:
        assert entered.wait(10) and not errors, errors
        beta = get_config_for_profile_home(two_profiles["beta"])
        assert beta["model"]["default"] == "model-beta"
        assert beta["model"]["provider"] == "anthropic"
        # A reference NO profile owns stays the literal ${VAR}, not "".
        assert beta["terminal"]["backend"] == "beta-term"
    finally:
        release.set()
        worker.join(10)


def test_unresolved_reference_stays_literal(two_profiles):
    from api.config import get_config_for_profile_home

    beta_home = two_profiles["beta"]
    (beta_home / "config.yaml").write_text(
        CONFIG_TEMPLATE
        + "auxiliary:\n  model:\n    provider: ${GATE_ABSENT}\n    default: ${GATE_ABSENT}\n",
        encoding="utf-8",
    )

    beta = get_config_for_profile_home(beta_home)

    # Not resolvable from beta's .env, and the process env value must not be used.
    assert beta["auxiliary"]["model"]["provider"] == "${GATE_ABSENT}"
    assert beta["auxiliary"]["model"]["default"] == "${GATE_ABSENT}"
    # Documented defaults still applied (shape parity with ambient get_config()).
    assert beta["agent"]["personalities"]
    # And the same document expanded under its own binding (the reference rule).
    assert _expand((beta_home / "config.yaml").read_text(encoding="utf-8"), beta_home)[
        "auxiliary"
    ]["model"]["provider"] == "${GATE_ABSENT}"
    assert os.environ["GATE_ABSENT"] == "process-leak"
