"""Shared launcher for node harnesses that embed extracted production source.

Several behavioural tests lift real functions out of ``static/*.js`` and splice
them into a DOM-shim harness. Handing that script to node as ``node -e <script>``
puts the WHOLE script into a single argv entry, and ``execve`` caps one argument
at ``MAX_ARG_STRLEN`` (128 KB on Linux). A routine change to an extracted
function therefore tips the harness over that edge and the test dies with
``OSError: [Errno 7] Argument list too long`` before node ever runs — an
infrastructure error that looks nothing like the behaviour it checks.

``run_node_script`` writes the script to a temporary ``.js`` file and runs
``node <path>`` instead. No argv budget, exactly the same code executes, and the
caller keeps the same return shape it had with ``subprocess.run``.

Use it for every harness whose script interpolates production source. A small
fixed driver (a few hundred bytes) can still use ``node -e`` safely; anything
that splices in a real function should come through here.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

# Linux caps one argv entry at MAX_ARG_STRLEN = 131072 bytes. Anything within a
# few KB of that is one production-code change away from E2BIG, so treat 100000
# as the "come through this helper" line.
ARGV_SAFE_BYTES = 100_000


def script_is_argv_safe(script: str) -> bool:
    """True when ``script`` is small enough to hand to ``node -e`` on Linux."""
    return len(script.encode("utf-8", "replace")) <= ARGV_SAFE_BYTES


def run_node_script(
    script: str,
    *,
    node: str = "node",
    cwd=None,
    timeout: int = 60,
    extra_args=(),
):
    """Run ``script`` under node via a temporary file, never through argv.

    ``extra_args`` are placed BEFORE the script path (``node <extra...> <path>``),
    so a heap cap such as ``--max-old-space-size`` survives the switch.
    """
    with tempfile.NamedTemporaryFile(
        "w", suffix=".js", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(script)
        script_path = fh.name
    try:
        return subprocess.run(
            [node, *extra_args, script_path],
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    finally:
        try:
            Path(script_path).unlink()
        except OSError:
            pass
