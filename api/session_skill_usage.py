"""Bounded, server-owned per-session skill provenance."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

MAX_SKILL_IDENTIFIERS = 64
MAX_SKILL_IDENTIFIER_LENGTH = 128
MAX_SKILL_COUNT = 1_000_000
_SKILL_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")


def normalize_skill_identifier(value) -> str | None:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if len(name) > MAX_SKILL_IDENTIFIER_LENGTH or not _SKILL_IDENTIFIER.fullmatch(name):
        return None
    return name


def normalize_skill_provenance(value) -> dict[str, int]:
    """Return only bounded canonical names and positive bounded counts."""
    if isinstance(value, Mapping):
        items = value.items()
    elif isinstance(value, (list, tuple, set)):
        items = ((item, 1) for item in value)
    else:
        return {}
    normalized: dict[str, int] = {}
    for raw_name, raw_count in items:
        name = normalize_skill_identifier(raw_name)
        if name is None or isinstance(raw_count, bool):
            continue
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if count <= 0:
            continue
        normalized[name] = min(MAX_SKILL_COUNT, count)
        if len(normalized) >= MAX_SKILL_IDENTIFIERS:
            break
    return normalized


def increment_skill_provenance(current, skill_names) -> dict[str, int]:
    result = normalize_skill_provenance(current)
    if isinstance(skill_names, str):
        skill_names = [skill_names]
    if not isinstance(skill_names, (list, tuple, set)):
        return result
    for raw_name in skill_names:
        name = normalize_skill_identifier(raw_name)
        if name is None or (name not in result and len(result) >= MAX_SKILL_IDENTIFIERS):
            continue
        result[name] = min(MAX_SKILL_COUNT, result.get(name, 0) + 1)
    return result


def successful_skill_names(result) -> tuple[str, ...]:
    """Extract canonical names from a successful Agent skill result only."""
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            return ()
    if (
        not isinstance(result, Mapping)
        or result.get("success") is not True
        or result.get("error")
    ):
        return ()
    name = normalize_skill_identifier(result.get("name"))
    return (name,) if name else ()


def copy_skill_provenance(value) -> dict[str, int]:
    return dict(normalize_skill_provenance(value))


def reset_skill_provenance() -> dict[str, int]:
    return {}


def compact_skill_provenance(value) -> dict[str, int]:
    return copy_skill_provenance(value)
