"""Profile-owned resolution of ``skills.external_dirs`` (#7842).

The Agent's ``agent.skill_utils.get_external_skills_dirs()`` reads the config of
the process's Hermes home and expands variables from the process environment.
WebUI profiles are per request, so a named profile's Skills panel would list the
root profile's external roots instead of its own. This module reads the request
profile's own ``config.yaml`` and resolves each entry as the Agent does
(``agent/skill_utils.py``: ``_config_str_list``, ``_expand_path``,
``_home_relative``), with variables bound by ``profile_env_binding``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from api import yaml_compat as yaml

logger = logging.getLogger(__name__)

# ``$VAR`` and ``${VAR}``, as ``os.path.expandvars`` accepts both.
_PATH_ENV_VAR = re.compile(r'\$(?:\{([^}]+)\}|([A-Za-z_][A-Za-z0-9_]*))')


def profile_external_skill_dirs(profile_home: Path) -> list[Path]:
    """Existing external skill roots configured for the profile at *profile_home*.

    Like the Agent: an unreadable or malformed config lists none, entries are
    expanded in a single pass (an unknown variable stays literal text), relative
    paths start at the profile home, the profile's own ``skills/`` dir and
    duplicates are dropped, and an entry that is not a directory is skipped.
    The live process environment is never read.
    """
    from api.paths import HOME
    from api.profiles import profile_env_binding

    config_path = profile_home / 'config.yaml'
    try:
        config = yaml.safe_load(config_path.read_bytes().decode('utf-8-sig'))
    except FileNotFoundError:
        return []
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
    if not entries:
        return []

    # The profile's own ${VAR} binding (#8129): a named profile gets only its own
    # .env; root gets the launch-time env under root's .env. HERMES_HOME is the
    # profile home. A profile .env does not supply HOME, so ~ and $HOME use the
    # WebUI's stable shell home.
    env = profile_env_binding(profile_home)
    env['HOME'] = str(HOME)

    def expand_var(match):
        return env.get(match.group(1) or match.group(2), match.group(0))

    local_skills = (profile_home / 'skills').resolve()
    roots: list[Path] = []
    for entry in entries:
        try:
            expanded = _PATH_ENV_VAR.sub(expand_var, entry)
            if expanded == '~' or expanded.startswith('~/'):
                expanded = str(HOME) + expanded[1:]
            else:
                expanded = os.path.expanduser(expanded)  # ~user, as the Agent's expanduser
            root = Path(expanded)
            if not root.is_absolute():
                root = profile_home / root
            root = root.resolve()
            if root == local_skills or root in roots or not root.is_dir():
                continue
        except (OSError, RuntimeError, ValueError):
            # RuntimeError: symlink loop before Python 3.13; ValueError: embedded NUL.
            logger.debug("Skipping unreadable external skills dir entry %r", entry)
            continue
        roots.append(root)
    return roots
