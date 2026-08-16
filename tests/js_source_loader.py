"""Shared, race-safe loader for JS-harness static-source reads.

The JS/DOM harness tests read ``static/ui.js`` (and sibling static sources)
and then extract function bodies with textual brace matching. Under
concurrent load (multiple full suites on one box), a read can observe a
truncated/partial view of the file, and the brace matcher then returns a
syntactically unrelated fragment instead of failing loudly. See issue #6972.

This module centralizes the read so every harness shares the same defense:

* **Per-attempt metadata validation** — each read attempt captures file
  metadata (size, inode, mtime) BEFORE and AFTER the read; both snapshots
  must match exactly. This prevents accepting a read that raced a rewrite.
* **Zero-length reject** — any read returning 0 bytes is explicitly rejected
  (empty files are never valid JS sources for our harnesses).
* **Bounded retry** — a short, partial read (file being rewritten, or an
  I/O-starved subprocess) is retried a few times instead of being accepted.
* **Fail closed** — if the read never stabilizes, ``SourceReadError`` is
  raised with an explicit message. The harness never sees a truncated
  fragment that brace matching could silently turn into a false pass/fail.
* **Optional tail sentinel** — callers that know the expected end-of-file
  marker (e.g. the last line of ``static/ui.js``) can require it. For
  extraction sources (where brace matching runs downstream), the tail
  sentinel is mandatory to ensure the entire source was captured.

A matching Node snippet for the ``node -e`` harnesses is available via
``node_validated_read_snippet()``.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

MAX_READ_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 0.02


class SourceReadError(RuntimeError):
    """Raised when a JS source file cannot be read to completion."""


@dataclass(frozen=True, slots=True)
class _FileIdentity:
    """Immutable file identity snapshot for race detection."""

    size: int
    ino: int
    mtime_ns: int

    @classmethod
    def from_stat(cls, st: os.stat_result) -> "_FileIdentity":
        return cls(size=st.st_size, ino=st.st_ino, mtime_ns=st.st_mtime_ns)


def _stat_identity(path: Path) -> _FileIdentity:
    """Capture a complete file identity snapshot."""
    return _FileIdentity.from_stat(path.stat())


def read_js_source(
    path: str | Path,
    *,
    expected_tail: str | None = None,
    require_tail: bool = False,
    max_attempts: int = MAX_READ_ATTEMPTS,
    retry_delay_seconds: float = RETRY_DELAY_SECONDS,
) -> str:
    """Read a JS source file to completion, retrying on unstable reads.

    Each attempt performs a *pre-read* stat, the read, then a *post-read*
    stat. The two identity snapshots (size, inode, mtime) must match
    exactly; otherwise the read raced a concurrent modification and is
    discarded. A zero-byte read is always rejected. When ``require_tail`` is
    true (for extraction sources fed to brace matching), the decoded text
    must end with ``expected_tail``; otherwise the attempt is treated as
    incomplete and retried/failed. If the file never reads consistently,
    :class:`SourceReadError` is raised instead of returning a fragment that
    downstream textual brace matching could silently mangle.

    Args:
        path: Path to the JS source file.
        expected_tail: Known end-of-file sentinel (e.g. last line of the
            source). Required when ``require_tail`` is true.
        require_tail: If true, the read must end with ``expected_tail``.
            Set this for sources that will undergo brace-matched extraction
            (ui.js, messages.js, etc.) to guarantee the full source was read.
        max_attempts: Maximum read attempts before failing closed.
        retry_delay_seconds: Base delay between attempts (linear backoff).

    Returns:
        The complete, validated file content as UTF-8 text.

    Raises:
        SourceReadError: If no attempt produces a stable, complete read.
    """
    p = Path(path)
    if require_tail and expected_tail is None:
        raise ValueError("require_tail=True requires expected_tail to be set")

    last_error: str | None = None
    for attempt in range(max_attempts):
        # Pre-read identity snapshot
        try:
            pre_identity = _stat_identity(p)
        except FileNotFoundError:
            last_error = f"file disappeared: {p}"
            time.sleep(retry_delay_seconds * (attempt + 1))
            continue

        # Reject zero-size files immediately (transient empty during rewrite)
        if pre_identity.size == 0:
            last_error = f"zero-byte file observed (transient empty during rewrite): {p}"
            time.sleep(retry_delay_seconds * (attempt + 1))
            continue

        # Read the file
        try:
            data = p.read_bytes()
        except OSError as e:
            last_error = f"read failed: {e}"
            time.sleep(retry_delay_seconds * (attempt + 1))
            continue

        # Explicit zero-length reject (a read that returned no bytes is
        # never a valid JS source — transient empty during rewrite).
        if len(data) == 0:
            last_error = f"zero-byte read (transient empty during rewrite): {p}"
            time.sleep(retry_delay_seconds * (attempt + 1))
            continue

        # Post-read identity snapshot
        try:
            post_identity = _stat_identity(p)
        except FileNotFoundError:
            last_error = f"file disappeared after read: {p}"
            time.sleep(retry_delay_seconds * (attempt + 1))
            continue

        # Identity must be stable across the read (no concurrent modification)
        if pre_identity != post_identity:
            last_error = (
                f"file identity changed during read (pre={pre_identity}, "
                f"post={post_identity}): {p}"
            )
            time.sleep(retry_delay_seconds * (attempt + 1))
            continue

        # Read must match the stable size
        read_size = len(data)
        if read_size != pre_identity.size:
            last_error = (
                f"read size {read_size} != stable stat size {pre_identity.size}: {p}"
            )
            time.sleep(retry_delay_seconds * (attempt + 1))
            continue

        # Decode and validate tail sentinel if required
        text = data.decode("utf-8")
        if require_tail:
            if not text.endswith(expected_tail):
                last_error = f"EOF sentinel missing (expected {expected_tail!r}): {p}"
                time.sleep(retry_delay_seconds * (attempt + 1))
                continue
        elif expected_tail is not None and not text.endswith(expected_tail):
            # Optional tail: if provided but not required, mismatch is a retry signal
            last_error = f"optional tail sentinel not reached: {p}"
            time.sleep(retry_delay_seconds * (attempt + 1))
            continue

        # Stable, complete read — success
        return text

    # All attempts exhausted
    raise SourceReadError(
        f"truncated read of {p}: {last_error or 'unknown error'} "
        f"after {max_attempts} attempts; refusing to extract from a partial source read"
    )


def node_validated_read_snippet() -> str:
    """Return a Node snippet that reads a file with the same defense.

    The returned JS defines ``readValidated(path, options?)`` which reads
    the file with per-attempt identity validation (size, inode, mtime),
    rejects zero-byte reads, retries up to ``MAX_READ_ATTEMPTS`` times on
    a partial/unstable read, and throws an explicit error instead of
    returning a truncated fragment. ``fs`` is required inside the function
    so the snippet never collides with a harness that already declares
    ``const fs = require('fs')``.

    The ``options`` object accepts:
    - ``expectedTail``: string sentinel that the content must end with
    - ``requireTail``: if true, missing sentinel fails the attempt
    """
    return (
        "\nconst MAX_READ_ATTEMPTS = " + str(MAX_READ_ATTEMPTS) + ";\n"
        "\n"
        "function readValidated(p, options = {}) {\n"
        "  const fs = require('fs');\n"
        "  const { expectedTail = null, requireTail = false } = options;\n"
        "\n"
        "  for (let attempt = 0; attempt < MAX_READ_ATTEMPTS; attempt++) {\n"
        "    // Pre-read identity\n"
        "    let preStat;\n"
        "    try {\n"
        "      preStat = fs.statSync(p);\n"
        "    } catch (e) {\n"
        "      // File disappeared, retry\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Zero-size reject\n"
        "    if (preStat.size === 0) {\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Read\n"
        "    let src;\n"
        "    try {\n"
        "      src = fs.readFileSync(p, 'utf8');\n"
        "    } catch (e) {\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Post-read identity\n"
        "    let postStat;\n"
        "    try {\n"
        "      postStat = fs.statSync(p);\n"
        "    } catch (e) {\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Identity must be stable (size, inode, mtime)\n"
        "    if (preStat.size !== postStat.size ||\n"
        "        preStat.ino !== postStat.ino ||\n"
        "        preStat.mtimeNs !== postStat.mtimeNs) {\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Read must match stable size\n"
        "    const readSize = Buffer.byteLength(src, 'utf8');\n"
        "    if (readSize !== preStat.size) {\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Explicit zero-length reject (defense in depth)\n"
        "    if (readSize === 0) {\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Tail sentinel validation\n"
        "    if (requireTail) {\n"
        "      if (expectedTail === null || !src.endsWith(expectedTail)) {\n"
        "        continue;\n"
        "      }\n"
        "    } else if (expectedTail !== null && !src.endsWith(expectedTail)) {\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Stable, complete read\n"
        "    return src;\n"
        "  }\n"
        "\n"
        "  throw new Error('source read incomplete: ' + p + ' (identity unstable or size mismatch after ' + MAX_READ_ATTEMPTS + ' attempts)');\n"
        "}\n"
    )