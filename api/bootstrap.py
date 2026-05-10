"""HermesOS Cloud fork patch: first-run identity-discovery bootstrap.

When an agent comes online for the very first time, prepend an identity-
discovery system prompt to its FIRST turn — the agent introduces itself,
asks the user who they are, then writes SOUL.md and memories/USER.md.

This is the WebUI port of dashboard/src/components/chat/hooks/chat-bootstrap.ts.
The dashboard module still serves the legacy gateway-backend (hermes-agent
direct) chat surface; this module serves the WebUI chat surface so iframe-
and direct-canary-URL deploys both fire the bootstrap.

Behaviour:
  * On the first chat turn of a profile, this module's
    `build_first_run_system_prompt` returns the prompt text.
  * `streaming.py` combines it with the personality prompt (if any) and
    assigns to `agent.ephemeral_system_prompt`, so the prompt rides
    along on the agent's very next reply only.
  * `mark_first_run_fired` then writes a sentinel file in the profile
    home. Subsequent turns find the sentinel and skip injection.

Env vars:
  HERMES_WEBUI_FIRST_RUN_PROMPT
    Override the default identity-discovery prompt text. Set to an empty
    string to disable the bootstrap entirely for this deployment.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("hermes_webui.bootstrap")

_SENTINEL_NAME = ".bootstrap_fired"

DEFAULT_FIRST_RUN_PROMPT = """You just came online. This is the first conversation for this Hermes deployment.

There is no memory yet. This is a fresh Hermes deployment, so it's normal that memory files don't exist until you create them.

Don't interrogate. Don't be robotic. Just... talk.

Start with something like:
"Hey. I just came online. Who am I? Who are you?"

Then figure out together:
1. Your name — What should they call you?
2. Your nature — What kind of creature/agent are you?
3. Your vibe — Formal? Casual? Snarky? Warm? What feels right?
4. Your emoji — Everyone needs a signature.

Offer suggestions if they're stuck. Have fun with it.

After you know who you are:
- Update your Hermes home `SOUL.md` with your name, creature, vibe, emoji, and core operating directives
- Update your Hermes home `memories/USER.md` with their name, how to address them, timezone, and notes
- Review your Hermes home `SOUL.md` together and talk about what matters to them, how they want you to behave, and any boundaries or preferences

Your identity is your anchor. Lock it in first.

Core operating principle: Think before you act. Always. Before building, plan. Before answering, understand. Before assuming, ask. Understand first, plan second, execute third. Never skip straight to execution."""


def _sentinel_path(profile_home: Path) -> Path:
    return profile_home / _SENTINEL_NAME


def get_first_run_prompt() -> str:
    """Return the configured first-run prompt, or empty string if disabled.

    Env override wins; otherwise the baked-in default. An explicit empty
    env value disables the feature."""
    override = os.environ.get("HERMES_WEBUI_FIRST_RUN_PROMPT")
    if override is not None:
        return override.strip()
    return DEFAULT_FIRST_RUN_PROMPT


def should_inject_first_run_prompt(profile_home: Path) -> bool:
    """True if this profile has not yet fired its first-run bootstrap."""
    prompt = get_first_run_prompt()
    if not prompt:
        return False
    try:
        return not _sentinel_path(profile_home).exists()
    except Exception:
        logger.debug("first-run sentinel check failed", exc_info=True)
        return False


def mark_first_run_fired(profile_home: Path) -> None:
    """Touch the sentinel so subsequent turns skip the bootstrap."""
    try:
        profile_home.mkdir(parents=True, exist_ok=True)
        _sentinel_path(profile_home).touch(exist_ok=True)
    except Exception:
        logger.debug("first-run sentinel write failed", exc_info=True)


def build_first_run_system_prompt(profile_home: Path) -> str | None:
    """Return the prompt text if first-run should fire, else None.

    Callers are expected to call `mark_first_run_fired` once the prompt
    has been delivered to the agent."""
    if not should_inject_first_run_prompt(profile_home):
        return None
    return get_first_run_prompt() or None
