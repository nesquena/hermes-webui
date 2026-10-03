"""Profile-bound view of Hermes Agent's external skill-directory lookup.

``skills.external_dirs`` is read by ``agent.skill_utils.get_external_skills_dirs()``,
which resolves the configured paths through the Hermes home: the context-local home
override when the Agent provides one, else ``os.environ['HERMES_HOME']``. The WebUI
resolves the request profile's local skills root itself, but that Agent helper expands
variables from the process environment, so a named profile could list another
profile's external roots
(and, mid-turn, the root profile could follow a streaming turn's mirrored home).

These helpers bind the request profile's home (root included, without touching
``os.environ``) and confirm the Agent's routing decision matches the profile the
WebUI resolved, so external roots are used only when they provably belong to the
request profile. Path expansion reads the bound profile's config and environment
directly, avoiding the Agent's process environment and shared expansion cache.
The scope shape mirrors ``api.mcp_runtime``.
"""

from __future__ import annotations

import logging
import re
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator

from api import yaml_compat as yaml

logger = logging.getLogger(__name__)

_PATH_ENV_VAR = re.compile(r'\$(?:\{([^}]+)\}|([A-Za-z_][A-Za-z0-9_]*))')


def profile_external_skill_dirs(profile_home: Path) -> list[Path]:
    """Resolve external roots without the Agent's process-env expansion/cache.

    Read fresh on each call: both config and profile .env can change, and the
    Agent's config-signature-only cache cannot distinguish expansion contexts.
    Never fall back to a streaming turn's process environment for path variables.
    """
    from api.paths import HOME, STARTUP_ENV
    from api.profiles import (
        _DEFAULT_HERMES_HOME,
        filter_runtime_env_for_gateway_parity,
        get_profile_runtime_env,
    )

    config_path = profile_home / 'config.yaml'
    if not config_path.exists():
        return []
    config = yaml.safe_load(config_path.read_text(encoding='utf-8')) or {}
    if not isinstance(config, dict):
        raise ValueError('Invalid profile skills config')
    skills = config.get('skills', {})
    if not isinstance(skills, dict):
        raise ValueError('Invalid profile skills config')
    entries = skills.get('external_dirs', []) or []
    if isinstance(entries, str):
        entries = [entries]
    if not isinstance(entries, list):
        raise ValueError('Invalid external skill directories')

    # Only the actual root home inherits launch variables. A named profile,
    # including one pinned as the process profile, keeps its own environment.
    # Compare resolved homes rather than a display name or the process anchor.
    env = dict(STARTUP_ENV) if profile_home.resolve() == _DEFAULT_HERMES_HOME.resolve() else {}
    env.update(filter_runtime_env_for_gateway_parity(get_profile_runtime_env(profile_home)))
    env['HERMES_HOME'] = str(profile_home)
    # Shell identity is not supplied by profile .env (gateway parity). Use the
    # WebUI's stable shell home for ~ and $HOME, never a live process-env read.
    env['HOME'] = str(HOME)

    def expand_var(match):
        key = match.group(1) or match.group(2)
        if key not in env:
            raise ValueError('Unresolved external skill path variable')
        return env[key]

    roots = []
    for entry in entries:
        if not isinstance(entry, str) or not entry.strip():
            continue
        expanded = _PATH_ENV_VAR.sub(expand_var, entry.strip())
        # Replacement values can themselves contain tokens (including cycles).
        # Never turn those unresolved tokens into literal filesystem roots.
        if _PATH_ENV_VAR.search(expanded):
            raise ValueError('Unresolved external skill path variable')
        if expanded == '~' or expanded.startswith('~/') or expanded.startswith('~\\'):
            expanded = str(HOME) + expanded[1:]
        elif expanded.startswith('~'):
            raise ValueError('Unsupported external skill path home')
        root = Path(expanded)
        if not root.is_absolute():
            root = profile_home / root
        root = root.resolve()
        if root.is_dir() and root not in roots:
            roots.append(root)
    return roots


# Scope label reported by the Skills API.
SCOPE_PROFILE = "profile"            # external roots bound to the request profile
SCOPE_LEGACY = "legacy_process"      # no routed predicate, but a bound home
SCOPE_UNAVAILABLE = "unavailable"    # profile scope could not be confirmed; roots withheld


@dataclass(frozen=True)
class SkillRuntimeScope:
    """How the current context sees the external skill-directory lookup.

    ``trusted``: external roots in this context belong to ``profile_home``.
    ``legacy``: the Agent has no routed predicate, but a home override is bound.
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
        # Agent predates profile scoping. A bound context-local home override is the
        # only proof that the Agent helper will read this request's profile; without
        # it the lookup falls back to the process-wide HERMES_HOME (which a streaming
        # turn may have mirrored to another profile), so fail closed rather than
        # expose another profile's external roots.
        if not override_bound:
            return SkillRuntimeScope(profile_home, trusted=False, legacy=False)
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
