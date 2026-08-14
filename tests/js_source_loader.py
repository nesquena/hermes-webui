"""Shared, race-safe loader for JS-harness static-source reads.

The JS/DOM harness tests read ``static/ui.js`` (and sibling static sources)
and then extract function bodies with textual brace matching. Under
concurrent load (multiple full suites on one box), a read can observe a
truncated/partial view of the file, and the brace matcher then returns a
syntactically unrelated fragment instead of failing loudly. See issue #6972.

This module centralizes the read so every harness shares the same defense:

* **Size validation** — the read is compared against the on-disk byte
  length (``stat``) before it is handed to extraction.
* **Bounded retry** — a short, partial read (file being rewritten, or an
  I/O-starved subprocess) is retried a few times instead of being accepted.
* **Fail closed** — if the read never stabilizes, ``SourceReadError`` is
  raised with an explicit message. The harness never sees a truncated
  fragment that brace matching could silently turn into a false pass/fail.
* **Optional tail sentinel** — callers that know the expected end-of-file
  marker (e.g. the last line of ``static/ui.js``) can require it.

A matching Node snippet for the ``node -e`` harnesses is available via
``node_validated_read_snippet()``.
"""

from __future__ import annotations

import time
from pathlib import Path

MAX_READ_ATTEMPTS = 5
RETRY_DELAY_SECONDS = 0.02


class SourceReadError(RuntimeError):
    """Raised when a JS source file cannot be read to completion."""


def read_js_source(
    path: str | Path,
    *,
    expected_tail: str | None = None,
    max_attempts: int = MAX_READ_ATTEMPTS,
    retry_delay_seconds: float = RETRY_DELAY_SECONDS,
) -> str:
    """Read a JS source file to completion, retrying on unstable reads.

    The read must match the on-disk byte length (and, when given, end with
    ``expected_tail``); otherwise it is treated as a partial read and retried
    with a short backoff. If the file never reads consistently,
    :class:`SourceReadError` is raised instead of returning a fragment that
    downstream textual brace matching could silently mangle.
    """
    p = Path(path)
    expected_size = p.stat().st_size
    last_size = -1
    for attempt in range(max_attempts):
        data = p.read_bytes()
        last_size = len(data)
        if last_size == expected_size:
            text = data.decode("utf-8")
            if expected_tail is None or text.endswith(expected_tail):
                return text
        time.sleep(retry_delay_seconds * (attempt + 1))
    raise SourceReadError(
        f"truncated read of {p}: expected {expected_size} bytes, "
        f"got {last_size} after {max_attempts} attempts; "
        f"refusing to extract from a partial source read"
    )


def node_validated_read_snippet() -> str:
    """Return a Node snippet that reads a file with the same defense.

    The returned JS defines ``readValidated(path)`` which reads the file,
    compares the byte length against ``fs.statSync``, retries up to
    ``MAX_READ_ATTEMPTS`` times on a partial read, and throws an explicit
    error instead of returning a truncated fragment. ``fs`` is required
    inside the function so the snippet never collides with a harness that
    already declares ``const fs = require('fs')``.
    """
    return f"""
const MAX_READ_ATTEMPTS = {MAX_READ_ATTEMPTS};
function readValidated(p){{
  const fs = require('fs');
  const expectedSize = fs.statSync(p).size;
  let src = null;
  for(let attempt = 0; attempt < MAX_READ_ATTEMPTS; attempt++){{
    src = fs.readFileSync(p, 'utf8');
    if(Buffer.byteLength(src, 'utf8') === expectedSize) return src;
  }}
  throw new Error('source read incomplete: ' + p + ' (' +
    Buffer.byteLength(src, 'utf8') + '/' + expectedSize + ' bytes)');
}}
"""
