"""Profile-bound view of Hermes Agent's external skill-directory lookup.

``skills.external_dirs`` is read by ``agent.skill_utils.get_external_skills_dirs()``,
which resolves the configured paths through the Hermes home: the context-local home
override when the Agent provides one, else ``os.environ['HERMES_HOME']``. The WebUI
resolves the request profile's local skills root itself, but that Agent helper reads
the *process* home, so a named profile could list another profile's external roots
(and, mid-turn, the root profile could follow a streaming turn's mirrored home).

These helpers bind the request profile's home (root included, without touching
``os.environ``) and confirm the Agent's routing decision matches the profile the
WebUI resolved, so external roots are used only when they provably belong to the
request profile. The shape mirrors ``api.mcp_runtime``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

# Scope label reported by the Skills API.
SCOPE_PROFILE = "profile"            # external roots bound to the request profile
SCOPE_LEGACY = "legacy_process"      # Agent predates the home override; process-wide lookup
SCOPE_UNAVAILABLE = "unavailable"    # profile scope could not be confirmed; roots withheld


@dataclass(frozen=True)
class SkillRuntimeScope:
    """How the current context sees the external skill-directory lookup.

    ``trusted``: external roots in this context belong to ``profile_home``.
    ``legacy``: the Agent has no context-local home override (process-wide lookup).
    """

    profile_home: Path
    trusted: bool
    legacy: bool

    @property
    def scope_label(self) -> str:
        if not self.trusted:
            return SCOPE_UNAVAILABLE
        return SCOPE_LEGACY if self.legacy else SCOPE_PROFILE


def _routing_view(profile_home: Path, override_bound: bool) -> SkillRuntimeScope:
    try:
        from agent.secret_scope import serves_routed_profile
        from hermes_constants import hermes_home_key
    except ImportError:
        # No routed-profile predicate: the Agent predates profile scoping.
        return SkillRuntimeScope(profile_home, trusted=True, legacy=True)
    from api.profiles import get_process_profile_home

    try:
        expected_routed = (
            hermes_home_key(profile_home) != hermes_home_key(get_process_profile_home())
        )
        # Without the override a routed profile cannot be served; with it, the Agent
        # must agree. It disagrees when a streaming turn has mirrored this profile's
        # home into os.environ['HERMES_HOME'] on an Agent without a pinned process home.
        if expected_routed and not override_bound:
            return SkillRuntimeScope(profile_home, trusted=False, legacy=False)
        if bool(serves_routed_profile()) != expected_routed:
            return SkillRuntimeScope(profile_home, trusted=False, legacy=False)
    except Exception:
        logger.debug(
            "Failed to resolve external-skill scope for %s", profile_home, exc_info=True
        )
        return SkillRuntimeScope(profile_home, trusted=False, legacy=False)
    return SkillRuntimeScope(profile_home, trusted=True, legacy=False)


@contextmanager
def skill_runtime_scope(
    purpose: str = "skill external-directory lookup",
) -> Generator[SkillRuntimeScope, None, None]:
    """Bind the active request profile for the Agent's external-root lookup.

    Installs the context-local Hermes-home override for the request profile,
    including the root profile, and restores it on exit, including on exceptions.
    Never mutates ``os.environ``.
    """
    from api.profiles import get_active_hermes_home, profile_env_for_active_request_readonly

    profile_home = Path(get_active_hermes_home())
    with profile_env_for_active_request_readonly(
        purpose, logger_override=logger, include_root=True
    ) as override_bound:
        yield _routing_view(profile_home, bool(override_bound))
