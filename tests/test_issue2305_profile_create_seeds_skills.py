# coding: utf-8
# Regression coverage for issue #2305 — seed bundled skills on profile creation.
#
# IMPORTANT: All filesystem operations use temporary directories only.
# Do NOT touch real ~/.hermes, real credentials, or real profile directories.
#
# Test strategy:
#   - Mock _DEFAULT_HERMES_HOME to a tmp_path so _resolve_base_hermes_home()
#     picks up the isolated root.
#   - Inject a mock 'hermes_cli.profiles' module directly into sys.modules so
#     that the `from hermes_cli.profiles import seed_profile_skills` inside
#     create_profile_api resolves to the mock (not the real module).
#   - Stub hermes_cli.profiles.create_profile to create the profile dir.
#   - Stub hermes_cli.profiles.seed_profile_skills to record calls.
#   - Verify the no-clone path calls seed exactly once with the resolved path.
#   - Verify the clone path calls seed zero times.
#   - Verify a raising seed still returns a profile dict (best-effort).
#
# Acceptance criteria:
#   1. create_profile_api(name, clone_from=None) → seed called once, path = profile_path.
#   2. create_profile_api(name, clone_from=<str>) → seed never called.
#   3. seed raising → profile dict returned, warning logged.

import logging
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# Import the module under test directly (isolated from any real HERMES_HOME env).
import api.profiles as profiles_mod


# ── Helpers ────────────────────────────────────────────────────────────────────

def _isolated_profiles_root(fake_home: Path) -> Path:
    return fake_home / 'profiles'


def _make_profile_dir(base: Path, name: str) -> Path:
    p = base / name
    p.mkdir(parents=True, exist_ok=True)
    return p


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_hermes_home(tmp_path, monkeypatch):
    # Point _DEFAULT_HERMES_HOME at an isolated temp directory so that
    # profile-path resolution does not touch the real ~/.hermes.
    fake_home = tmp_path / '.hermes'
    fake_home.mkdir(parents=True)
    monkeypatch.setenv('HERMES_BASE_HOME', str(fake_home))
    monkeypatch.setattr(profiles_mod, '_DEFAULT_HERMES_HOME', fake_home)
    return fake_home


def _install_hermes_cli_profiles_mock(create_impl, seed_impl):
    # Inject a mock 'hermes_cli.profiles' module directly into sys.modules.
    # This is the only way to intercept `from hermes_cli.profiles import X`
    # inside create_profile_api — patch.dict(sys.modules, ...) only modifies
    # existing keys and cannot add new ones.
    mock = ModuleType('hermes_cli.profiles')
    mock.create_profile = create_impl
    mock.seed_profile_skills = seed_impl
    sys.modules['hermes_cli'] = ModuleType('hermes_cli')
    sys.modules['hermes_cli.profiles'] = mock
    return mock


def _remove_hermes_cli():
    for key in list(sys.modules):
        if key == 'hermes_cli' or key.startswith('hermes_cli.'):
            del sys.modules[key]


# Module references saved at import time so we can restore the real hermes_cli
# after each test that overwrites sys.modules['hermes_cli.profiles'].  This
# prevents the `FallbackDoesNotCrash` tests from finding a deleted entry and
# incorrectly skipping.
_real_hermes_cli = sys.modules.get('hermes_cli')
_real_hermes_cli_profiles = sys.modules.get('hermes_cli.profiles')


def _restore_real_hermes_cli():
    if _real_hermes_cli is not None:
        sys.modules['hermes_cli'] = _real_hermes_cli
    if _real_hermes_cli_profiles is not None:
        sys.modules['hermes_cli.profiles'] = _real_hermes_cli_profiles


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestNoCloneSeedsSkills:
    def test_seed_called_once_with_resolved_path(self, fake_hermes_home):
        calls = []

        def fake_create(name, **kw):
            _make_profile_dir(_isolated_profiles_root(fake_hermes_home), name)

        def fake_seed(profile_path, quiet=None):
            calls.append({'profile_path': profile_path, 'quiet': quiet})

        _remove_hermes_cli()
        _install_hermes_cli_profiles_mock(fake_create, fake_seed)

        try:
            with patch.object(profiles_mod, 'list_profiles_api', return_value=[]):
                result = profiles_mod.create_profile_api('testprofile')
        finally:
            _remove_hermes_cli()
            _restore_real_hermes_cli()

        # seed_profile_skills must have been called exactly once.
        assert len(calls) == 1, f'Expected 1 seed call, got {len(calls)}: {calls}'
        # quiet=True is required.
        assert calls[0]['quiet'] is True
        # Path must be the resolved profile directory under the fake hermes home.
        expected_path = _isolated_profiles_root(fake_hermes_home) / 'testprofile'
        assert calls[0]['profile_path'] == expected_path, (
            f'Expected seed path {expected_path}, got {calls[0]}'
        )
        # Profile dict must be returned.
        assert result['name'] == 'testprofile'


class TestCloneSkipsSeeding:
    def test_seed_not_called_when_clone_from_is_set(self, fake_hermes_home):
        calls = []

        def fake_create(name, clone_from=None, **kw):
            _make_profile_dir(_isolated_profiles_root(fake_hermes_home), name)

        def fake_seed(profile_path, quiet=None):
            calls.append({'profile_path': profile_path, 'quiet': quiet})

        _remove_hermes_cli()
        _install_hermes_cli_profiles_mock(fake_create, fake_seed)

        try:
            with patch.object(profiles_mod, 'list_profiles_api', return_value=[]):
                result = profiles_mod.create_profile_api(
                    'clonedprofile', clone_from='sourceprofile'
                )
        finally:
            _remove_hermes_cli()
            _restore_real_hermes_cli()

        # seed must not be called at all when cloning.
        assert calls == [], f'seed_profile_skills was called during clone: {calls}'
        # Profile dict must still be returned.
        assert result['name'] == 'clonedprofile'


class TestSeedFailureIsBestEffort:
    def test_seed_raising_logs_warning_and_still_returns_profile(self, fake_hermes_home, caplog):
        import logging as std_logging

        def fake_create(name, **kw):
            _make_profile_dir(_isolated_profiles_root(fake_hermes_home), name)

        def fake_seed(profile_path, quiet=None):
            raise RuntimeError('Bundled skill installation failed')

        _remove_hermes_cli()
        _install_hermes_cli_profiles_mock(fake_create, fake_seed)

        try:
            with caplog.at_level(std_logging.WARNING):
                with patch.object(profiles_mod, 'list_profiles_api', return_value=[]):
                    result = profiles_mod.create_profile_api('failprofile')
        finally:
            _remove_hermes_cli()
            _restore_real_hermes_cli()

        # A warning must have been logged naming the profile.
        warning_messages = [rec.message for rec in caplog.records if rec.levelno == std_logging.WARNING]
        assert any('failprofile' in msg for msg in warning_messages), (
            f'No warning mentioning profile name found. Logged: {warning_messages}'
        )
        # Profile dict is returned (best-effort).
        assert result['name'] == 'failprofile'
        assert 'path' in result


class TestHermesCliUnavailableFallbackDoesNotCrash:
    def test_fallback_create_still_produces_profile_dict(self, fake_hermes_home):
        # Simulate hermes_cli being present but create_profile raising ImportError
        # (e.g. in a Docker/standalone environment where the profiles sub-module
        # fails to load). This exercises the _create_profile_fallback path and
        # confirms the new seed block does not interfere with it.
        #
        # We cannot permanently delete hermes_cli.profiles from sys.modules (it
        # may be needed by other tests in this process), so we raise ImportError
        # at the call site by temporarily replacing create_profile on the real
        # module with a function that raises ImportError.

        real_mod = sys.modules.get('hermes_cli.profiles')
        if real_mod is None:
            # hermes_cli.profiles was already cleaned up by a prior test in this
            # process — skip rather than failing with a confusing assertion.
            pytest.skip('hermes_cli.profiles not in sys.modules (cleaned up by prior test)')

        orig_create = real_mod.create_profile
        real_mod.create_profile = MagicMock(side_effect=ImportError('hermes_cli profiles unavailable'))
        try:
            with patch.object(profiles_mod, 'list_profiles_api', return_value=[]):
                result = profiles_mod.create_profile_api('isolatedprofile')
        finally:
            real_mod.create_profile = orig_create

        # Fallback path must have created the profile and returned a dict.
        assert result['name'] == 'isolatedprofile'
        expected_path = _isolated_profiles_root(fake_hermes_home) / 'isolatedprofile'
        assert Path(result['path']) == expected_path

    def test_seed_unavailable_logs_debug_without_crashing(self, fake_hermes_home, caplog):
        import logging as std_logging

        def fake_create(name, **kw):
            _make_profile_dir(_isolated_profiles_root(fake_hermes_home), name)

        # Grab references BEFORE we overwrite sys.modules — once saved here we
        # can safely restore them in finally regardless of what happens in between.
        real_mod = sys.modules.get('hermes_cli.profiles')
        real_hermes_cli = sys.modules.get('hermes_cli')
        if real_mod is None or real_hermes_cli is None:
            pytest.skip('hermes_cli.profiles not in sys.modules (cleaned up by prior test)')

        # We need hermes_cli.profiles.seed_profile_skills to not exist so that
        # `from hermes_cli.profiles import seed_profile_skills` raises ImportError.
        # We achieve this by putting a mock module with no seed attr in sys.modules
        # and restoring the real module in the finally block.
        _remove_hermes_cli()
        mock = ModuleType('hermes_cli.profiles')
        mock.create_profile = fake_create
        # NO seed_profile_skills attribute — absence causes ImportError in the
        # import statement inside create_profile_api.
        fake_hermes_cli = ModuleType('hermes_cli')
        sys.modules['hermes_cli'] = fake_hermes_cli
        sys.modules['hermes_cli.profiles'] = mock

        try:
            with caplog.at_level(std_logging.DEBUG):
                with patch.object(profiles_mod, 'list_profiles_api', return_value=[]):
                    result = profiles_mod.create_profile_api('nohermesprofile')
        finally:
            # Restore the real modules so subsequent tests can use them.
            _remove_hermes_cli()
            sys.modules['hermes_cli'] = real_hermes_cli
            sys.modules['hermes_cli.profiles'] = real_mod

        # Profile is still created.
        assert result['name'] == 'nohermesprofile'
        # Debug log about unavailable seed_profile_skills.
        debug_messages = [rec.message for rec in caplog.records if rec.levelno == std_logging.DEBUG]
        assert any('seed_profile_skills' in msg for msg in debug_messages), (
            f'No debug log about unavailable seed_profile_skills. Logged: {debug_messages}'
        )