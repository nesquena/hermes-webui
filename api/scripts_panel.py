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

import os
import re
import stat as stat_module
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
    """Containment guard for a script name coming from outside this module.

    Rejects empty, over-long, path separators, NUL, control characters and the
    dot entries. This is a SECURITY boundary, not a filename policy: a script
    the installed Agent will run must not disappear from the panel because its
    name is not a URL slug (#7685 finding 3). ``my job.py`` and ``résumé.sh``
    are perfectly good scripts, so anything that survives the traversal checks
    below is accepted and the transport encodes it.

    The checks are ordered cheapest-first and every one of them is a traversal
    or control-character test — never a character-class whitelist.
    """
    if not name or len(name) > 128:
        return False
    if "/" in name or "\\" in name or "\0" in name:
        return False
    if name in (".", ".."):
        return False
    # A leading dot would hide the entry from the Agent's own conventions and
    # from a plain directory listing, so it is not a runnable script name.
    if name.startswith("."):
        return False
    # Control characters have no business in a filename and would corrupt the
    # HTTP response that carries it.
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
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


def _base_open_flags() -> int:
    """Open flags for the scripts *directory* fd.

    Fail closed: O_NOFOLLOW and O_DIRECTORY are load-bearing for containment
    (the leaf ``openat`` resolves relative to this fd, so an ancestor symlink
    or a swapped-in file must never slip through), and there is no fallback
    that preserves the guarantee on a platform without them. We therefore
    raise ``OSError`` instead of silently substituting zero for a missing
    flag, which would drop containment (#7685 finding 2).
    """
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("O_NOFOLLOW not supported; refusing scripts directory open")
    if not hasattr(os, "O_DIRECTORY"):
        raise OSError("O_DIRECTORY not supported; refusing scripts directory open")
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _leaf_open_flags() -> int:
    """Open flags for the scripts *leaf*.

    Same fail-closed rule for O_NOFOLLOW: without it a symlink escape is
    possible, so a missing flag is a refusal, not a silent zero.  O_NONBLOCK
    IS the one tolerated absence — its job here is only to make a pathological
    FIFO ``os.open`` return instead of blocking, and ``fstat`` still rejects
    the FIFO afterwards.  O_CLOEXEC is best-effort and optional.
    """
    if not hasattr(os, "O_NOFOLLOW"):
        raise OSError("O_NOFOLLOW not supported; refusing scripts leaf open")
    return (
        os.O_RDONLY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )


def _open_script_for_read(base: Path, name: str) -> tuple[int, os.stat_result] | None:
    """Acquire a validated descriptor for ``base/name``.

    Returns ``(fd, stat)`` for a REGULAR FILE that provably lives inside
    ``base``, or ``None`` for every rejection. The caller owns the fd and
    must close it on every exit path.

    This is the containment boundary all reads go through, and it exists to
    close three windows (#7685 review):

    * **Symlink escape.** Following a symlink that points outside the
      scripts directory returned that target's description (and, for
      ``read_script``, its contents). The fd is opened with ``O_NOFOLLOW``;
      ``fstat`` then proves the opened object is a regular file, so a race
      that swaps the leaf for a device, FIFO or directory fails rather than
      blocks or dumps.
    * **Leaf/ancestor swap.** The previous helper resolved the path, checked
      it, and then reopened it by name — two independent lookups an attacker
      could swap between. One ``openat`` on a directory fd pins BOTH the
      ancestor and the leaf: the kernel resolves the final component
      relative to the fd we already hold, so nothing outside it can be
      reached by a mid-flight rename.
    * **Growth after the size check.** ``read_script`` stat'ed the size and
      then read the file by name; a file that grew in between returned
      over-cap content flagged ``too_large: false``. Here ``fstat`` on the
      open fd is the metadata the size decision is made from, and the read
      itself is bounded to the cap at physical I/O.

    Descriptor accounting (#7685 finding 1): every fd acquired here is
    released on every exit — the base fd is always closed, and a leaf fd that
    was opened but then rejected (non-regular object, ``fstat`` failure) is
    closed too before ``None`` is returned.  Only the success path hands the
    leaf fd to the caller, which then owns it.  A FIFO named ``*.py`` is
    opened with ``O_NONBLOCK`` so ``os.open`` returns instead of blocking,
    and is then rejected by the regular-file check.
    """
    if not _is_safe_script_name(name):
        return None
    base_fd = -1
    file_fd = -1
    accepted = False
    try:
        base_fd = os.open(base, _base_open_flags())
        st = os.fstat(base_fd)
        if not stat_module.S_ISDIR(st.st_mode):
            return None
        file_fd = os.open(name, _leaf_open_flags(), dir_fd=base_fd)
        fst = os.fstat(file_fd)
        if not stat_module.S_ISREG(fst.st_mode):
            return None
        # A hardlink out of the directory still has nlink > 1; nothing here
        # can detect that, but the name is confined and the content is the
        # user's own profile, so containment of the LOOKUP is what matters.
        accepted = True
        return file_fd, fst
    except OSError:
        # Missing capability flag, permission error, symlink escape
        # (ELOOP from O_NOFOLLOW), non-directory base, missing name, or an
        # fstat failure all funnel here as a refusal.
        return None
    finally:
        if base_fd >= 0:
            os.close(base_fd)
        if not accepted and file_fd >= 0:
            # The leaf was opened but then rejected: release it so it cannot
            # leak one descriptor per list request.
            _close_quietly(file_fd)


def _close_quietly(fd: int) -> None:
    if fd is None or fd < 0:
        return
    try:
        os.close(fd)
    except OSError:
        pass


class _BoundedRead:
    """Honest result of a bounded read.

    ``ok`` is False when the underlying ``os.read`` raised; ``truncated`` is
    True when the budget (``limit``) was exhausted and there might be bytes
    left we did not read; ``content`` is whatever was read (possibly empty);
    ``detail`` carries the exception text when ``ok`` is False.
    """

    __slots__ = ("ok", "truncated", "content", "detail")

    def __init__(
        self, ok: bool, truncated: bool, content: bytes, detail: str = ""
    ) -> None:
        self.ok = ok
        self.truncated = truncated
        self.content = content
        self.detail = detail


def _read_bounded(fd: int, limit: int) -> _BoundedRead:
    """Read at most ``limit`` bytes from ``fd`` (an open regular file).

    Loops until EOF or the cap, so a short read does not silently truncate
    the preview.  The result reports rather than hides two failure modes
    (#7685 finding 3): a read that raises becomes ``ok=False`` instead of an
    empty "complete" read, and stopping at the cap is ``truncated=True``
    instead of being indistinguishable from EOF.
    """
    chunks: list[bytes] = []
    remaining = limit
    failed = False
    detail = ""
    # #7685 finding 3 (SHOULD-FIX): "stopped at the cap" is not the same as
    # "there is more". A file of EXACTLY `limit` bytes reads to remaining == 0
    # and then hits EOF, so it is complete — the old `remaining == 0` test
    # reported it as truncated. Truncation means the cap was reached AND at
    # least one more byte exists, so the loop below records whether the read
    # that emptied `remaining` was an EOF or another chunk.
    hit_eof = False
    while remaining > 0:
        try:
            chunk = os.read(fd, min(remaining, 65536))
        except OSError as exc:
            failed = True
            detail = f"{exc.__class__.__name__}: {exc}"
            break
        if not chunk:
            hit_eof = True
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    # The cap truncates only when it stopped the read before EOF. A file of
    # exactly `limit` bytes fills the budget and then hits EOF on the NEXT read,
    # so after the loop empties `remaining` we probe once more: an empty read
    # proves the file ended exactly at the cap and is complete.
    if not failed and remaining == 0 and limit > 0:
        try:
            hit_eof = not os.read(fd, 1)
        except OSError:
            # The probe itself failed; treat the cap as truncating rather than
            # claiming a completeness we could not observe.
            hit_eof = False
    truncated = not failed and not hit_eof and remaining == 0 and limit > 0
    return _BoundedRead(
        ok=not failed,
        truncated=truncated,
        content=b"".join(chunks),
        detail=detail,
    )


def _read_description(path: Path) -> str:
    """Best-effort description from a path, via the bounded-read helper."""
    base = scripts_dir()
    acquired = _open_script_for_read(base, path.name)
    if acquired is None:
        return ""
    fd, _st = acquired
    try:
        return _read_description_bytes(_read_bounded(fd, 4096).content, path.suffix.lower())
    finally:
        _close_quietly(fd)


def _read_description_bytes(raw: bytes, suffix: str) -> str:
    """Derive the one-line description from an already-bounded byte buffer."""
    head = raw[:4096].decode("utf-8", errors="replace")
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

    Containment before content (#7685 finding 1): a candidate is only
    described after it is proven to be a regular file INSIDE the scripts
    directory, so a symlink pointing outside the target is skipped rather
    than reported with its target's description.
    """
    base = scripts_dir()
    out: list[dict] = []
    if base.is_dir():
        for entry in _safe_iterdir(base):
            if entry.suffix.lower() not in _SCRIPT_EXTS:
                continue
            # Acquire a validated descriptor FIRST: no preview metadata is
            # read from anything that is not a regular file pinned inside
            # the scripts directory.
            acquired = _open_script_for_read(base, entry.name)
            if acquired is None:
                continue
            fd, st = acquired
            try:
                raw = _read_bounded(fd, 4096).content
                out.append(
                    {
                        "name": entry.name,
                        "size": st.st_size,
                        "modified": int(st.st_mtime),
                        "description": _read_description_bytes(
                            raw, entry.suffix.lower()
                        ),
                    }
                )
            finally:
                _close_quietly(fd)
    out.sort(key=lambda e: e["name"].lower())
    return {
        "scripts": out,
        "directory": str(base),
        "exists": base.is_dir(),
    }


def read_script(name: str) -> dict | None:
    """Return ``{name, content, size, modified}`` for the named script,
    or ``None`` if the name is invalid or the file does not exist.

    The 256 KiB cap is enforced at physical I/O (#7685 finding 2): the size
    decision uses ``fstat`` on the open descriptor and the read is bounded
    to the same cap, so a file that grows between the check and the read
    cannot return over-cap content flagged ``too_large: false``.
    """
    base = scripts_dir()
    acquired = _open_script_for_read(base, name)
    if acquired is None:
        return None
    fd, st = acquired
    try:
        if st.st_size > _MAX_SCRIPT_BYTES:
            # Refuse to inline a multi-hundred-KiB script in the panel.
            return {
                "name": name,
                "too_large": True,
                "size": st.st_size,
            }
        res = _read_bounded(fd, _MAX_SCRIPT_BYTES)
        if not res.ok:
            # A read that raised must surface as an error, not as an empty
            # "complete" script (#7685 finding 3).
            return {
                "name": name,
                "error": "read_failed",
                "detail": res.detail,
                "size": st.st_size,
            }
        content = res.content.decode("utf-8", errors="replace")
        return {
            "name": name,
            "content": content,
            "size": st.st_size,
            "modified": int(st.st_mtime),
            "too_large": False,
            "truncated": res.truncated,
        }
    finally:
        _close_quietly(fd)


def _safe_iterdir(base: Path) -> Iterable[Path]:
    """Generator wrapper that fail-closes on permission errors."""
    try:
        yield from base.iterdir()
    except (PermissionError, FileNotFoundError, NotADirectoryError):
        return
