"""
Regression test for issue #4586: the v0.51.528 isolated-profile-mode feature (#2698)
must NOT engage from the HERMES_HOME *shape* alone.

The bug: `_is_isolated_profile_mode()` inferred "isolated mode" purely from a
`~/.hermes/profiles/<name>` shaped HERMES_HOME. But the Hermes Agent launcher exports
exactly that shape for ANY active named (non-default) profile in a normal single-user
deployment — so an ordinary single user running under a named profile (e.g. `webui`) was
wrongly treated as an intentional multi-user isolation deployment. Symptoms (v0.51.528+):
  - the Profiles tab listed only the active profile, and
  - switching to any other profile was blocked with PermissionError
      ("Profile switching is not allowed in isolated profile mode.").

The fix (#4586): isolated mode requires an EXPLICIT opt-in — HERMES_WEBUI_ISOLATED_PROFILE —
as the primary gate (default OFF). The profile-shaped HERMES_HOME is only a secondary
requirement. A normal named-profile launch (shape set, flag unset) must therefore stay in
normal multi-profile mode.

These tests deliberately set ONLY the profile shape (no flag) and assert NORMAL behavior —
the exact thing that regressed. They would FAIL on v0.51.528..current and PASS after the fix.
"""

import logging
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import api.profiles as _profiles_mod
from api.profiles import (
    _is_isolated_profile_mode,
    _isolated_profile_opt_in,
    switch_profile,
)


@pytest.fixture(autouse=True)
def _clear_cache_and_force_flag_off(monkeypatch):
    """Every test here runs WITHOUT the isolated opt-in — the regressed single-user case.

    We explicitly clear HERMES_WEBUI_ISOLATED_PROFILE so the suite's ambient env can't
    accidentally enable isolation and mask the regression.
    """
    monkeypatch.delenv("HERMES_WEBUI_ISOLATED_PROFILE", raising=False)
    monkeypatch.setattr(_profiles_mod, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "")
    _profiles_mod._LIST_PROFILES_CACHE = None
    yield
    _profiles_mod._LIST_PROFILES_CACHE = None


@pytest.fixture
def named_profile_home():
    """A normal single-user layout: base ~/.hermes with several profiles, active = `webui`.

    The active profile's home is `~/.hermes/profiles/webui` — exactly the
    `*/profiles/<name>` shape the Hermes Agent launcher exports for a named profile, and
    exactly what the #4586 false-positive keyed off.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        home = Path(tmpdir) / ".hermes"
        profiles_root = home / "profiles"
        profiles_root.mkdir(parents=True)
        for name in ("alpha", "beta", "webui"):
            p = profiles_root / name
            for subdir in ("memories", "sessions", "skills", "skins",
                           "logs", "plans", "workspace", "cron"):
                (p / subdir).mkdir(parents=True, exist_ok=True)
        yield {"base": home, "active": profiles_root / "webui", "profiles_root": profiles_root}


class TestIssue4586NamedProfileIsNotIsolated:
    """A named-profile single-user launch (shape set, flag unset) is NOT isolated mode."""

    def test_shape_alone_does_not_enable_isolated_mode(self, named_profile_home):
        """The core regression: */profiles/<name> WITHOUT the flag → NOT isolated."""
        active = named_profile_home["active"]
        assert active.parent.name == "profiles"  # has the shape that used to false-positive
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(active)}, clear=False):
            with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(active)):
                assert _isolated_profile_opt_in() is False
                assert _is_isolated_profile_mode() is False, (
                    "named-profile shape must NOT engage isolated mode without the explicit "
                    "HERMES_WEBUI_ISOLATED_PROFILE opt-in (#4586)"
                )

    def test_profiles_tab_is_not_clamped_to_single_profile(self, named_profile_home):
        """Regressed symptom #1: Profiles tab showed only the active profile.

        The regression was driven entirely by `list_profiles_api()` taking its
        isolated-mode early-return (`if _is_isolated_profile_mode(): return [only active]`).
        We assert the GATE that controls that branch is off — environment-independent,
        unlike exercising the full hermes_cli-backed discovery machinery (which depends on
        real base-home/profiles-root dirs that differ under CI). With the gate off,
        list_profiles_api() takes its normal multi-profile path instead of clamping to one.
        """
        active = named_profile_home["active"]
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(active)}, clear=False):
            with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(active)):
                # The isolated early-return in list_profiles_api() is gated on this:
                assert _is_isolated_profile_mode() is False, (
                    "list_profiles_api() must NOT take its single-profile early-return for "
                    "a normal named profile — that early-return is what hid all but the "
                    "active profile in the Profiles tab (#4586)"
                )

    def test_profile_shape_without_opt_in_warns_once(self, named_profile_home, monkeypatch, caplog):
        """A profile-shaped startup HERMES_HOME without opt-in emits one diagnostic."""
        active = named_profile_home["active"]
        monkeypatch.setattr(
            _profiles_mod,
            "_ISOLATED_PROFILE_SHAPE_WITHOUT_OPT_IN_WARNING_EMITTED",
            False,
        )

        with mock.patch.dict(os.environ, {"HERMES_HOME": str(active)}, clear=False):
            with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(active)):
                with caplog.at_level(logging.WARNING, logger="api.profiles"):
                    assert _is_isolated_profile_mode() is False
                    assert _is_isolated_profile_mode() is False

        warnings = [
            record.getMessage()
            for record in caplog.records
            if "HERMES_WEBUI_ISOLATED_PROFILE was not enabled at startup" in record.getMessage()
        ]
        assert len(warnings) == 1

    def test_switching_to_another_profile_is_allowed(self, named_profile_home):
        """Regressed symptom #2: switching was blocked with PermissionError."""
        base = named_profile_home["base"]
        active = named_profile_home["active"]
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(active)}, clear=False):
            with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(active)):
                with mock.patch("api.profiles._DEFAULT_HERMES_HOME", base):
                    with mock.patch("api.profiles._resolve_base_hermes_home", return_value=base):
                        # Must NOT raise PermissionError (the regression). process_wide=False
                        # keeps this from mutating global interpreter state during the test.
                        try:
                            switch_profile("alpha", process_wide=False)
                        except PermissionError as e:
                            pytest.fail(
                                f"profile switching must work for a normal named profile; "
                                f"got PermissionError: {e} (#4586)"
                            )


class TestIssue4586ExplicitOptInStillWorks:
    """The explicit opt-in still engages isolated mode (multi-user deployments unaffected)."""

    @pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
    def test_flag_plus_shape_enables_isolated_mode(self, named_profile_home, flag):
        active = named_profile_home["active"]
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(active),
                                          "HERMES_WEBUI_ISOLATED_PROFILE": flag}, clear=False):
            with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(active)):
                with mock.patch(
                    "api.profiles._INITIAL_ISOLATED_PROFILE_OPT_IN",
                    flag.strip().lower(),
                ):
                    assert _isolated_profile_opt_in() is True
                    assert _is_isolated_profile_mode() is True, (
                        f"explicit opt-in ({flag!r}) + profile shape must still engage isolated mode"
                    )

    def test_flag_without_profile_shape_stays_off(self, named_profile_home):
        """Secondary requirement: a stray flag without a profile-shaped HERMES_HOME = OFF."""
        base = named_profile_home["base"]  # base ~/.hermes, NOT a */profiles/<name> path
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(base),
                                          "HERMES_WEBUI_ISOLATED_PROFILE": "1"}, clear=False):
            with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(base)):
                with mock.patch("api.profiles._INITIAL_ISOLATED_PROFILE_OPT_IN", "1"):
                    assert _is_isolated_profile_mode() is False, (
                        "flag set but HERMES_HOME is the base home → isolation must stay off"
                    )

    @pytest.mark.parametrize("flag", ["", "0", "false", "no", "off", "  "])
    def test_falsey_flag_values_are_off(self, named_profile_home, flag):
        active = named_profile_home["active"]
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(active),
                                          "HERMES_WEBUI_ISOLATED_PROFILE": flag}, clear=False):
            with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(active)):
                with mock.patch(
                    "api.profiles._INITIAL_ISOLATED_PROFILE_OPT_IN",
                    flag.strip().lower(),
                ):
                    assert _is_isolated_profile_mode() is False, (
                        f"falsey flag {flag!r} must not engage isolated mode"
                    )


class TestIssue4590ProfileEnvCannotDisableIsolation:
    """#4590: a pinned profile's own .env must NOT be able to turn isolation OFF.

    _reload_dotenv() copies a profile's .env into os.environ. Before the fix, a
    contained user could put HERMES_WEBUI_ISOLATED_PROFILE=0 in their profile .env
    and escape isolation. The operator flag is now snapshotted at startup.
    """

    def test_profile_env_cannot_clear_isolated_flag(self, named_profile_home, monkeypatch):
        from api.profiles import _reload_dotenv, _PROTECTED_ENV_KEYS

        assert "HERMES_WEBUI_ISOLATED_PROFILE" in _PROTECTED_ENV_KEYS

        active = named_profile_home["active"]
        monkeypatch.setattr(_profiles_mod, "_INITIAL_ISOLATED_PROFILE_OPT_IN", "1")
        # Operator opts the deployment into isolation.
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(active),
                                          "HERMES_WEBUI_ISOLATED_PROFILE": "1"}, clear=False):
            with mock.patch("api.profiles._INITIAL_HERMES_HOME", str(active)):
                assert _is_isolated_profile_mode() is True

                # Prove the startup snapshot is the authority even if a future
                # live-env path regresses and lets the profile .env mutate the
                # operator flag in os.environ.
                monkeypatch.setattr(_profiles_mod, "_PROTECTED_ENV_KEYS", frozenset())
                assert _is_isolated_profile_mode() is True

                # The contained user plants HERMES_WEBUI_ISOLATED_PROFILE=0 in their .env.
                (active / ".env").write_text(
                    "HERMES_WEBUI_ISOLATED_PROFILE=0\nSOME_OTHER_KEY=ok\n", encoding="utf-8"
                )
                try:
                    _reload_dotenv(active)
                    # Even if live env is overwritten, the startup snapshot keeps
                    # the deployment isolated.
                    assert os.environ.get("HERMES_WEBUI_ISOLATED_PROFILE") == "0", (
                        "test setup should prove live os.environ was mutated by profile .env"
                    )
                    assert _is_isolated_profile_mode() is True, (
                        "#4590: a profile .env must not be able to disable isolation"
                    )
                    # Non-protected keys still load normally.
                    assert os.environ.get("SOME_OTHER_KEY") == "ok"
                finally:
                    try:
                        os.environ.pop("SOME_OTHER_KEY", None)
                    except Exception:
                        pass

    def test_runtime_env_path_also_strips_protected_flag(self, named_profile_home):
        """#4589 (runtime path): get_profile_runtime_env must NOT carry the protected
        isolation flag out of a profile's .env (the background-worker/streaming path
        also applies runtime env to os.environ — same escape, second door)."""
        from api.profiles import get_profile_runtime_env, _BLOCKED_RUNTIME_ENV_KEYS

        assert "HERMES_WEBUI_ISOLATED_PROFILE" in _BLOCKED_RUNTIME_ENV_KEYS

        active = named_profile_home["active"]
        (active / ".env").write_text(
            "HERMES_WEBUI_ISOLATED_PROFILE=0\nMY_PROFILE_KEY=value1\n", encoding="utf-8"
        )
        env = get_profile_runtime_env(active)
        assert "HERMES_WEBUI_ISOLATED_PROFILE" not in env, (
            "#4589: runtime env must not carry the isolation flag out of a profile .env"
        )
        # Non-protected profile keys still project into runtime env.
        assert env.get("MY_PROFILE_KEY") == "value1"
