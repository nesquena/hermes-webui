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
* **Mandatory tail sentinel for extraction sources** — callers that feed a
  source to brace matching or ``eval`` must read it through
  :func:`read_extraction_source`, which enforces ``require_tail=True`` with
  the expected end-of-file marker from the :data:`EXTRACTION_TAILS`
  registry. The registry is pinned by a regression asserting each entry is
  a real suffix of its static file, so drift fails loudly instead of
  silently weakening the sentinel.

A matching Node snippet for the ``node -e`` harnesses is available via
``node_validated_read_snippet()``; the Node side must use the same
registry through ``node_validated_read_options()`` so both paths enforce
the same EOF sentinel.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

MAX_READ_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 0.02

#: Expected end-of-file sentinels for the static sources that undergo
#: brace-matched extraction or evaluation downstream (issue #6972). A read
#: of one of these sources must reach its registered tail or the attempt is
#: discarded — a stable partial rewrite (file rewritten to a smaller but
#: consistent size, identity stable within each attempt) is otherwise
#: indistinguishable from a complete file by size/identity alone.
#:
#: Each value must be an exact suffix of the real ``static/<name>`` file.
#: ``test_extraction_tail_registry_matches_real_static_files`` in
#: ``tests/test_js_source_loader.py`` asserts that, so editing a registered
#: file's final line fails loudly until the registry is updated
#: deliberately.
EXTRACTION_TAILS: dict[str, str] = {
    "ui.js": "  return names;\n}\n",
    "messages.js": "}\n\n// ── Panel navigation (Chat / Tasks / Skills / Memory) ──\n",
    "sessions.js": "  navigateSession(e.key==='j'?1:-1);\n});\n",
    "assistant_turn_anchors.js": "  });\n})();\n",
    "extension_settings.js": "  primeFromStatus(window.__HERMES_EXTENSION_CONFIG__||{});\n})();\n",
    "i18n.js": "// Apply saved locale immediately so there's no flash of English on reload.\nloadLocale();\n",
    "icons.js": '       + `style="display:inline-block;vertical-align:-0.15em;flex-shrink:0">${p}</svg>`;\n}\n',
}


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
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as e:
            # Non-UTF-8 bytes are a rewrite-in-progress signal (or a corrupt
            # file); never leak a bare UnicodeDecodeError to the harness —
            # fail closed via SourceReadError like every other unstable read.
            last_error = f"decode failed (non-UTF-8 bytes, likely mid-rewrite): {e}"
            time.sleep(retry_delay_seconds * (attempt + 1))
            continue
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


def read_extraction_source(
    path: str | Path,
    *,
    max_attempts: int = MAX_READ_ATTEMPTS,
    retry_delay_seconds: float = RETRY_DELAY_SECONDS,
) -> str:
    """Read a static source that will be brace-matched or evaluated.

    This is the required entry point for extraction/evaluation harnesses
    (issue #6972): it looks up the source's expected EOF sentinel in
    :data:`EXTRACTION_TAILS` and enforces ``require_tail=True``, so a
    stable partial rewrite (smaller but consistent file, identity stable
    within each attempt) still fails closed instead of feeding a garbled
    function slice to brace matching.

    Args:
        path: Path to the JS source file (basename must be registered in
            ``EXTRACTION_TAILS``).
        max_attempts: Maximum read attempts before failing closed.
        retry_delay_seconds: Base delay between attempts (linear backoff).

    Returns:
        The complete, tail-validated file content as UTF-8 text.

    Raises:
        ValueError: If the source basename has no registered extraction tail
            (a new extraction source must be added to the registry first —
            never silently read without the sentinel).
        SourceReadError: If no attempt produces a stable, complete read.
    """
    p = Path(path)
    try:
        expected_tail = EXTRACTION_TAILS[p.name]
    except KeyError:
        raise ValueError(
            f"no registered extraction tail for {p.name!r}; add it to "
            "EXTRACTION_TAILS in tests/js_source_loader.py (and keep the "
            "drift regression green) before reading it as an extraction source"
        ) from None
    return read_js_source(
        p,
        expected_tail=expected_tail,
        require_tail=True,
        max_attempts=max_attempts,
        retry_delay_seconds=retry_delay_seconds,
    )


def node_validated_read_options(path: str | Path) -> str:
    """Return a JS options object literal enforcing the registered EOF tail.

    Extraction harnesses built on ``node_validated_read_snippet()`` must
    read their sources with ``readValidated(path, <this>)`` so the Node
    path enforces the *same* tail sentinel as the Python path (parity for
    issue #6972). The options come from :data:`EXTRACTION_TAILS`, so there
    is exactly one registry for both languages.

    Args:
        path: Path whose basename must be registered in ``EXTRACTION_TAILS``.

    Returns:
        A JS object literal, e.g. ``{ expectedTail: "}\\n", requireTail: true }``.

    Raises:
        ValueError: If the basename has no registered extraction tail.
    """
    p = Path(path)
    try:
        tail = EXTRACTION_TAILS[p.name]
    except KeyError:
        raise ValueError(
            f"no registered extraction tail for {p.name!r}; add it to "
            "EXTRACTION_TAILS in tests/js_source_loader.py"
        ) from None
    return "{ expectedTail: " + json.dumps(tail) + ", requireTail: true }"


def node_validated_read_snippet() -> str:
    """Return a Node snippet that reads a file with the same defense.

    The returned JS defines ``readValidated(path, options?)`` which reads
    the file with per-attempt identity validation (size, inode, mtime —
    all via ``fs.statSync(p, { bigint: true })`` so the mtimeNs field is
    actually present and the comparison is real, not dead code), rejects
    zero-byte reads, retries up to ``MAX_READ_ATTEMPTS`` times on a
    partial/unstable read with the same linear settle delay as the Python
    loader, and throws an explicit error instead of returning a truncated
    fragment. If a stat result lacks the BigInt fields (e.g. a mock or a
    legacy stat call without ``{ bigint: true }``), the snippet throws
    immediately — a missing ``mtimeNs`` must never pass the identity check
    silently. ``fs`` is required inside the function so the snippet never
    collides with a harness that already declares ``const fs = require('fs')``.

    The ``options`` object accepts:
    - ``expectedTail``: string sentinel that the content must end with
    - ``requireTail``: if true, missing sentinel fails the attempt

    Extraction harnesses should pass the options returned by
    ``node_validated_read_options()`` so the Node path enforces the same
    registered EOF tail as the Python path.
    """
    return (
        "\nconst MAX_READ_ATTEMPTS = " + str(MAX_READ_ATTEMPTS) + ";\n"
        "\n"
        "function readValidated(p, options = {}) {\n"
        "  const fs = require('fs');\n"
        "  const { expectedTail = null, requireTail = false } = options;\n"
        "\n"
        "  // Synchronous linear settle delay (mirrors the Python loader's\n"
        "  // per-attempt backoff; gives a concurrent rewrite time to finish).\n"
        "  const _settleDelayMs = 20;\n"
        "  const _sleep = (ms) => { Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms); };\n"
        "\n"
        "  // BigInt stat fields are mandatory: a stat without { bigint: true }\n"
        "  // has no mtimeNs, which would silently turn the identity check into\n"
        "  // dead code. Fail loudly instead of letting the check pass blindly.\n"
        "  const _requireBigIntStat = (st) => {\n"
        "    if (st.mtimeNs === undefined || typeof st.size !== 'bigint') {\n"
        "      throw new Error('source stat missing BigInt size/mtimeNs (fs.statSync must use { bigint: true }): ' + p);\n"
        "    }\n"
        "    return st;\n"
        "  };\n"
        "\n"
        "  for (let attempt = 0; attempt < MAX_READ_ATTEMPTS; attempt++) {\n"
        "    if (attempt > 0) _sleep(_settleDelayMs * attempt);\n"
        "\n"
        "    // Pre-read identity (bigint: size/ino/mtimeNs are BigInts)\n"
        "    let preStat;\n"
        "    try {\n"
        "      preStat = _requireBigIntStat(fs.statSync(p, { bigint: true }));\n"
        "    } catch (e) {\n"
        "      // File disappeared (or BigInt stat unavailable) — retry/throw\n"
        "      if (e instanceof Error && e.message.indexOf('missing BigInt') >= 0) throw e;\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Zero-size reject\n"
        "    if (preStat.size === 0n) {\n"
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
        "      postStat = _requireBigIntStat(fs.statSync(p, { bigint: true }));\n"
        "    } catch (e) {\n"
        "      // File disappeared (or BigInt stat unavailable) — retry/throw\n"
        "      if (e instanceof Error && e.message.indexOf('missing BigInt') >= 0) throw e;\n"
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
        "    // Read must match stable size (BigInt byte count vs BigInt stat)\n"
        "    const readSize = BigInt(Buffer.byteLength(src, 'utf8'));\n"
        "    if (readSize !== preStat.size) {\n"
        "      continue;\n"
        "    }\n"
        "\n"
        "    // Explicit zero-length reject (defense in depth)\n"
        "    if (readSize === 0n) {\n"
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