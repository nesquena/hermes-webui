# ── Skill usage reader (read-only) ──
# Note: .usage.json is written by hermes-agent (tools/skill_usage.py).
# WebUI only reads to display usage stats in Insights page.

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_USAGE_FILE = ".usage.json"


def read_skill_usage(skills_dir: Path) -> dict:
    """Read the current .usage.json.

    Returns the raw nested dict ``{skill_name: {use_count: N, view_count: N, ...}}``
    or an empty dict when the file does not exist or is corrupt.
    """
    usage_path = skills_dir / _USAGE_FILE
    if not usage_path.exists():
        return {}
    try:
        raw = usage_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
        logger.debug("Unexpected .usage.json format, resetting: %s", raw[:200])
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read .usage.json: %s", exc)
        return {}