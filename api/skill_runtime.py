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

# The tokenizer ``os.path.expandvars`` compiles, per platform, so the profile-bound
# expansion gives the same result the Agent would get from the process environment
# (#9321). They are ``posixpath._varpattern`` and ``ntpath._varpattern``; both are
# byte-identical on CPython 3.11-3.14, so the behaviour does not depend on the
# interpreter the WebUI happens to run under.
#   POSIX:   $name over ``\w+`` (so a bare ``$1abc`` is a reference) and ${name}.
#   Windows: additionally %NAME% (hyphens included), bare hyphenated $NAME,
#            ``$$`` -> ``$`` and ``%%`` -> ``%``, and single-quoted runs untouched.
_POSIX_EXPAND_VAR = re.compile(r'\$(\w+|\{[^}]*\}?)', re.ASCII)
_WINDOWS_EXPAND_VAR = re.compile(r"'[^']*'?|%(%|[^%]*%?)|\$(\$|[-\w]+|\{[^}]*\}?)", re.ASCII)


def expand_path_vars(value: str, env: dict[str, str], *, windows: bool | None = None) -> str:
    """Expand *value* exactly as ``os.path.expandvars`` does, using only *env*.

    *env* is the caller's binding — this helper never reads the live process
    environment, so a named profile keeps resolving against its own ``.env``.
    The expansion is a single pass: a name the binding does not hold stays
    literal text, and a value is not expanded again.

    *windows* selects the ``ntpath`` rules; it defaults to the running platform
    (``os.name == 'nt'``), so both behaviours are testable from one host.
    """
    if windows is None:
        windows = os.name == 'nt'
    pattern = _WINDOWS_EXPAND_VAR if windows else _POSIX_EXPAND_VAR

    def expand_var(match):
        group = match.lastindex
        if group is None:
            return match.group(0)  # Windows: a single-quoted run stays as it is
        name = match.group(group)
        if windows and group == 1:
            if name == '%':
                return name  # %% is one literal percent sign
            if not name.endswith('%'):
                return match.group(0)  # unclosed %NAME
            name = name[:-1]
        elif name == '$':
            return name  # Windows: $$ is one literal dollar sign
        elif name.startswith('{'):
            if not name.endswith('}'):
                return match.group(0)  # unclosed ${NAME
            name = name[1:-1]
        return env.get(name, match.group(0))

    return pattern.sub(expand_var, value)


def profile_external_skill_dirs(profile_home: Path, *, windows: bool | None = None) -> list[Path]:
    """Existing external skill roots configured for the profile at *profile_home*.

    Like the Agent: an unreadable or malformed config lists none, entries are
    expanded in a single pass as ``os.path.expandvars`` would (an unknown
    variable stays literal text), relative paths start at the profile home, the
    profile's own ``skills/`` dir and duplicates are dropped, and an entry that
    is not a directory is skipped. The live process environment is never read.
    *windows* overrides the platform the expansion follows (tests, and a caller
    that knows better than ``os.name``).
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

    local_skills = (profile_home / 'skills').resolve()
    roots: list[Path] = []
    for entry in entries:
        try:
            expanded = expand_path_vars(entry, env, windows=windows)
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
