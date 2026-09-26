"""Hermes scripts panel: list and read-only view of ``~/.hermes/scripts/``.

Issue #2316 requested a Scripts subtab inside the Tasks panel so users can
browse the ``--no-agent`` cron-job script directory without leaving the
WebUI. Read-only is the first slice -- edit-from-WebUI is a much larger
surface and intentionally out of scope.

Discovery: the active profile's ``scripts/`` directory under
``HERMES_HOME``. Both ``.py`` and ``.sh`` files are listed; orphan
scripts (not referenced by any cron job) are shown too, since that is
the most useful surface to expose.

Security: every read goes through ``_safe_join`` which fail-closes on
path traversal, symlink escape, and empty input. The route layer
validates the slug format before this module is even called.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

# Python module docstring triple-quote (with optional leading whitespace).
_PY_DOCSTRING_RE = re.compile(
    r'^\s*(?:r|u|f|rf|fr)?\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')',
    re.DOTALL,
)
# Leading shebang (only matches the first line; no capture).
_SHEBANG_RE = re.compile(r"^#!.*\n")
# Shell comment block at the top of the file (consecutive lines starting with #).
_SH_COMMENT_BLOCK_RE = re.compile(r"^(#.*\n)+", re.MULTILINE)

_SCRIPT_EXTS = {".py", ".sh"}
_MAX_SCRIPT_BYTES = 256 * 1024  # 256 KiB cap per read


def scripts_dir() -> Path:
    """Return the active profile's ``scripts/`` directory.

    Resolved against the active Hermes home so multi-profile installs
    only see scripts owned by the current profile.
    """
    from api.profiles import get_active_hermes_home

    return get_active_hermes_home() / "scripts"


def _is_safe_script_name(name: str) -> bool:
    """Slug-format guard for ``?name=`` query params.

    Reject empty, slash, backslash, dot-dot, control characters, and
    anything that isn't a portable POSIX filename. The route layer
    should call this before any filesystem read.
    """
    if not name or len(name) > 128:
        return False
    if "/" in name or "\\" in name or "\0" in name:
        return False
    if name in (".", ".."):
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        return False
    return True


def _safe_join(base: Path, name: str) -> Path | None:
    """Resolve ``base / name`` and fail-closed if the result escapes
    ``base`` after symlink resolution.

    Returns ``None`` on any traversal attempt so the caller can surface
    a clean 400 instead of leaking the parent listing.
    """
    if not _is_safe_script_name(name):
        return None
    try:
        candidate = (base / name).resolve(strict=False)
        base_resolved = base.resolve(strict=False)
        # Use is_relative_to (Python 3.9+) so a sibling named
        # ``scripts-evil`` cannot pass the prefix check.
        if not candidate.is_relative_to(base_resolved):
            return None
    except (OSError, ValueError):
        return None
    return candidate


def _read_description(path: Path) -> str:
    """Best-effort description string from the script's leading comment.

    Python: triple-quoted docstring at the top of the module.
    Shell: leading consecutive ``# ...`` comment block, with the
    shebang line stripped first so a ``#!/bin/bash`` line doesn't
    count as a comment.

    Returns the first non-empty line, trimmed. Empty string when no
    description can be derived (caller decides whether to display
    a placeholder or hide the field).
    """
    try:
        # Read up to 4 KiB; descriptions are always near the top.
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(4096)
    except OSError:
        return ""
    suffix = path.suffix.lower()
    if suffix == ".py":
        # Skip shebang then look for the first triple-quoted string.
        head = _SHEBANG_RE.sub("", head, count=1)
        m = _PY_DOCSTRING_RE.search(head)
        if not m:
            return ""
        body = m.group(1).strip()
    elif suffix == ".sh":
        # Skip the shebang line so the leading # comment block starts
        # at the first human comment, not the interpreter directive.
        head = _SHEBANG_RE.sub("", head, count=1)
        m = _SH_COMMENT_BLOCK_RE.match(head)
        if not m:
            return ""
        body = m.group(0).strip()
    else:
        return ""
    # Take the first non-empty line so a multi-line docstring still
    # surfaces a one-line summary in the list view.
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line[:280]
    return ""


def list_scripts() -> dict:
    """Return ``{"scripts": [...], "directory": str}``.

    Each script entry: ``{name, path, size, modified, description}``.
    Entries are sorted by name (case-insensitive) for a stable UI order.
    """
    base = scripts_dir()
    out: list[dict] = []
    if base.is_dir():
        for entry in _safe_iterdir(base):
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in _SCRIPT_EXTS:
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            out.append(
                {
                    "name": entry.name,
                    "size": stat.st_size,
                    "modified": int(stat.st_mtime),
                    "description": _read_description(entry),
                }
            )
    out.sort(key=lambda e: e["name"].lower())
    return {
        "scripts": out,
        "directory": str(base),
        "exists": base.is_dir(),
    }


def read_script(name: str) -> dict | None:
    """Return ``{name, content, size, modified}`` for the named script,
    or ``None`` if the name is invalid or the file does not exist.
    """
    base = scripts_dir()
    path = _safe_join(base, name)
    if path is None or not path.is_file():
        return None
    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > _MAX_SCRIPT_BYTES:
        # Refuse to inline a multi-hundred-KiB script in the panel.
        return {
            "name": name,
            "too_large": True,
            "size": size,
        }
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return {
        "name": name,
        "content": content,
        "size": size,
        "modified": int(path.stat().st_mtime),
        "too_large": False,
    }


def _safe_iterdir(base: Path) -> Iterable[Path]:
    """Generator wrapper that fail-closes on permission errors."""
    try:
        yield from base.iterdir()
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return
