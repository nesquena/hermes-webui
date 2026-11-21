"""Profile-owned resolution of ``skills.external_dirs``.

``agent.skill_utils.get_external_skills_dirs()`` resolves the configured paths
through the Hermes home (the context-local home override when the Agent provides
one, else ``os.environ['HERMES_HOME']``) and expands variables from the process
environment, so a named profile could list another profile's external roots
(and, mid-turn, the root profile could follow a streaming turn's mirrored home).

The WebUI therefore resolves the request profile's external roots itself, from
that profile's own ``config.yaml`` and runtime environment. Nothing here calls the
Agent helper, so no Agent home binding or routing check is needed.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from api import yaml_compat as yaml

logger = logging.getLogger(__name__)

_PATH_ENV_VAR = re.compile(r'\$(?:\{([^}]+)\}|([A-Za-z_][A-Za-z0-9_]*))')


def profile_external_skill_dirs(profile_home: Path) -> list[Path]:
    """Resolve external roots without the Agent's process-env expansion/cache.

    Read fresh on each call: both config and profile .env can change, and the
    Agent's config-signature-only cache cannot distinguish expansion contexts.
    Never fall back to a streaming turn's process environment for path variables.
    Config handling follows the Agent's ``_load_raw_config()`` / ``_skills_cfg()``
    / ``_config_str_list()``: a malformed config lists no external roots, and an
    entry that does not resolve to a readable directory is skipped. A config that
    cannot be read at all raises ``OSError`` so the caller reports an incomplete
    list that the browser retries.
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
    data = config_path.read_bytes()
    try:
        config = yaml.safe_load(data.decode('utf-8-sig'))
    except Exception:
        logger.debug("Could not read skills config %s", config_path, exc_info=True)
        return []
    skills = config.get('skills') if isinstance(config, dict) else None
    raw = skills.get('external_dirs') if isinstance(skills, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    entries = [entry for entry in (str(item).strip() for item in raw) if entry]

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
        # Single pass, as the Agent's os.path.expandvars: an unknown variable stays
        # literal text (it is never looked up in the live process environment),
        # and a `$` inside a replacement value is not expanded again.
        key = match.group(1) or match.group(2)
        return env.get(key, match.group(0))

    def resolve(entry):
        expanded = _PATH_ENV_VAR.sub(expand_var, entry)
        if expanded == '~' or expanded.startswith('~/') or expanded.startswith('~\\'):
            expanded = str(HOME) + expanded[1:]
        elif expanded.startswith('~'):
            # ~user names another account's home, as the Agent's expanduser does.
            expanded = os.path.expanduser(expanded)
        root = Path(expanded)
        if not root.is_absolute():
            root = profile_home / root
        return root.resolve()

    # The Agent drops an entry naming the local skills dir already scanned.
    local_skills = (profile_home / 'skills').resolve()
    roots = []
    for entry in entries:
        try:
            root = resolve(entry)
            if not root.is_dir():
                continue
        except (OSError, RuntimeError, ValueError):
            # RuntimeError: symlink loop in Path.resolve() before Python 3.13;
            # ValueError: an embedded NUL character.
            logger.debug("Skipping unreadable external skills dir entry %r", entry)
            continue
        if root != local_skills and root not in roots:
            roots.append(root)
    return roots


# Scope label reported by the Skills API.
SCOPE_PROFILE = "profile"            # external roots resolved from the request profile
SCOPE_UNAVAILABLE = "unavailable"    # config.yaml unreadable; external roots withheld
