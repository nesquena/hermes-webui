"""Immutable startup snapshot of operator-only environment controls.

These controls define the deployment boundary rather than a user preference:
whether the voice-config API accepts writes at all, and whether the TTS SSRF
guard may be widened to reach a private LAN target. They are read ONCE, at
import time, from the environment the operator started the process with.

Why a snapshot instead of a live ``os.environ`` read:

* ``profiles._reload_dotenv()`` projects the active profile's dotenv file into
  the live process environment. A profile able to set these keys would grant
  itself operator authority — turning on voice-config writes and widening the
  SSRF allowlist to hosts of its own choosing.
* That projection is process-global. The widened value would not stay with the
  profile that set it; it would also apply to concurrent requests serving other
  profiles.

This mirrors how ``HERMES_WEBUI_ISOLATED_PROFILE`` is handled (#4589/#4590):
operator posture comes from the environment the process was started in, never
from a file a contained user can write. ``profiles._PROTECTED_ENV_KEYS`` and
``profiles._BLOCKED_RUNTIME_ENV_KEYS`` carry the same set, so such a key never
reaches ``os.environ`` from a profile in the first place — this snapshot is the
second line of defence, and the one that still holds if a new projection path
is added later without consulting those lists.

What still works, and what does not
----------------------------------

The snapshot is of the environment the PROCESS STARTED WITH, which is not the
same as "no dotenv file may contribute". ``ctl.sh`` loads the repo dotenv file
and then ``${HERMES_HOME}/.env`` into its own shell before exec'ing the server
(``_load_repo_dotenv_preserving_env`` / ``_load_hermes_dotenv``), so values an
operator keeps in ``~/.hermes/.env`` are already in the environment by the time
this module is imported and keep working unchanged over that launcher.

What cannot reach these controls is a dotenv file belonging to a profile the
server SWITCHES TO later: that path runs through ``_reload_dotenv()`` long after
import, and is the one a contained user can write.

Two consequences worth stating plainly:

* Under a launcher that does not pre-load ``~/.hermes/.env`` (a bare
  ``python3 bootstrap.py``), these three controls must come from the service
  environment — systemd unit, container env, shell export.
* If the server is started with ``HERMES_HOME`` pointing at a profile home,
  that profile's dotenv file IS the startup environment. That is the pinned
  single-profile deployment posture (``HERMES_WEBUI_ISOLATED_PROFILE``), where
  the profile is the deployment; it is not a way for one profile to raise its
  own privileges while another is being served.
"""

from __future__ import annotations

import os

# Operator/deployment posture. Never settable, clearable, or alterable by a
# profile. Keep in sync with the two lists in ``api/profiles.py``; the test
# ``test_operator_only_keys_are_in_both_profile_lists`` enforces that.
OPERATOR_ONLY_ENV_KEYS = frozenset({
    'HERMES_WEBUI_ISOLATED_PROFILE',
    'HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE',
    'HERMES_WEBUI_TTS_ALLOW_LAN',
    'HERMES_WEBUI_TTS_ALLOW_HOSTS',
})

_TRUTHY = {'1', 'true', 'yes', 'on'}

# Captured at import. ``api/profiles.py`` imports this module at module level,
# so the snapshot is always taken before that module's dotenv projection can
# run for the first time.
_STARTUP_ENV: dict[str, str] = {
    key: os.environ.get(key, '') for key in OPERATOR_ONLY_ENV_KEYS
}


def operator_env(key: str) -> str:
    """Return the startup value of an operator-only control.

    Raises ``KeyError`` for anything not declared operator-only, so a caller
    cannot quietly route an ordinary setting through this module and inherit
    immutability it was never granted.
    """
    if key not in OPERATOR_ONLY_ENV_KEYS:
        raise KeyError(f"{key} is not an operator-only control")
    return _STARTUP_ENV.get(key, '')


def operator_env_truthy(key: str) -> bool:
    """True when the operator set ``key`` to a truthy value at startup."""
    return operator_env(key).strip().lower() in _TRUTHY


def _set_startup_env_for_tests(**overrides: str) -> dict[str, str]:
    """Replace snapshot values for a test, returning the previous mapping.

    Tests cannot re-import this module to re-take the snapshot, and monkey-
    patching ``os.environ`` deliberately no longer has any effect here — that
    is the property under test. Restore with
    ``_set_startup_env_for_tests(**previous)``.
    """
    previous = dict(_STARTUP_ENV)
    for key, value in overrides.items():
        if key not in OPERATOR_ONLY_ENV_KEYS:
            raise KeyError(f"{key} is not an operator-only control")
        _STARTUP_ENV[key] = value
    return previous
