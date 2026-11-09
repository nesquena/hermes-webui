#!/usr/bin/env python3
"""Rewrite existing WebUI session files to drop persisted inline image bytes.

The save-boundary projection (api/session_image_persistence_override.py) only
bounds NEW writes. Sessions written before PERSISTENCE_OVERRIDE_VERSION 1.0.0 can
still carry megabytes of ``data:image/...;base64,...`` in ``messages`` and
``context_messages``, which is what made history requests time out.

This tool is opt-in and conservative:

* REQUIRES A QUIESCENT WRITER: stop the WebUI service (or accept that a
  concurrent save may be overwritten) before using ``--apply``. The tool
  re-reads each file and aborts that file if it changed between the read and the
  replace, but it does not take the per-session mutation lock, so it cannot
  serialise against a live writer.
* default is a dry run; nothing is written without ``--apply``
* every changed file is first copied to ``<name>.preimage-migration.bak``
  (never overwriting an existing backup)
* the rewrite is temp-file + fsync + atomic replace
* row counts are asserted unchanged before the replace
* a file is skipped when the projection would change nothing

Usage:
    python scripts/migrate_persisted_image_payloads.py                # dry run
    python scripts/migrate_persisted_image_payloads.py --apply
    python scripts/migrate_persisted_image_payloads.py --apply --session-dir DIR
    python scripts/migrate_persisted_image_payloads.py --apply --include-context

By default only the display transcript (``messages``) is rewritten.
``context_messages`` is the provider replay prefix, and the WebUI contract keeps
those rows byte-identical across a save/load round trip so the next model call
replays exactly what the user sent. ``--include-context`` opts into bounding it
too; the model then receives a marker for historical images instead of bytes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.session_image_persistence_override import (  # noqa: E402
    PERSISTENCE_OVERRIDE_VERSION,
    project_message_payloads,
)


def _default_session_dir() -> Path:
    """Resolve the session directory exactly as the server does.

    Mirrors ``api/config.py``: ``HERMES_WEBUI_STATE_DIR`` wins; otherwise the
    state home is ``HERMES_HOME`` (or the platform default) plus ``webui``.
    Hard-coding ``~/.hermes/webui`` meant that on an installation with a custom
    ``HERMES_HOME`` and no ``HERMES_WEBUI_STATE_DIR`` this tool silently rewrote
    a DIFFERENT installation's sessions while leaving the intended files alone.
    """
    state = os.getenv("HERMES_WEBUI_STATE_DIR", "").strip()
    if state:
        return Path(state).expanduser().resolve() / "sessions"
    hermes_home = os.getenv("HERMES_HOME", "").strip()
    state_home = (
        Path(hermes_home).expanduser() if hermes_home else Path.home() / ".hermes"
    )
    return state_home / "webui" / "sessions"


def _iter_session_files(session_dir: Path):
    for path in sorted(session_dir.glob("*.json")):
        if path.name.startswith(("_", ".")) or path.name.endswith(".bak"):
            continue
        yield path


def _projection_keys(include_context: bool):
    return ("messages", "context_messages") if include_context else ("messages",)


def _stat_signature(path: Path):
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def _detect_indent(raw: str) -> int:
    """Return the file's existing indentation width, or 0 when it is compact.

    ``json.dumps(..., indent=2)`` was applied unconditionally, so a session stored
    in the compact single-line layout came back pretty-printed and GREW even while
    megabytes of base64 were removed. Measured on real data: f906676da374.json
    589,946 -> 636,306 bytes (+46,360) purely from the encoder change. Detecting the
    existing layout keeps every rewrite <= its preimage.
    """
    for line in raw.split("\n")[1:]:
        stripped = line.lstrip(" ")
        if stripped:
            return len(line) - len(stripped) or 2
    return 0


def _encode_like(raw: str, projected: dict) -> str:
    """Serialize ``projected`` in the same layout as ``raw``.

    Layout is preserved rather than normalized: the migration's contract is to
    bound image bytes, not to reformat the file. ``ensure_ascii`` also follows the
    original so a file that stored escaped non-ASCII does not grow from
    re-escaping (and a file storing raw UTF-8 is not unexpectedly escaped).
    """
    ascii_only = not any(ord(ch) > 127 for ch in raw)
    kwargs = {"ensure_ascii": ascii_only}
    indent = _detect_indent(raw)
    if indent:
        kwargs["indent"] = indent
    else:
        kwargs["separators"] = (",", ":")
    encoded = json.dumps(projected, **kwargs)
    try:
        encoded.encode("utf-8")
    except UnicodeEncodeError:
        encoded = json.dumps(projected, **{**kwargs, "ensure_ascii": True})
    return encoded


def migrate_file(path: Path, *, apply: bool, include_context: bool = False) -> dict:
    """Rewrite one session file. Returns a per-file report dict."""
    report = {"file": path.name, "changed": False, "error": None,
              "bytes_before": 0, "bytes_after": 0}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        report["error"] = f"read_failed:{exc}"
        return report
    except UnicodeDecodeError as exc:
        # A file that is not valid UTF-8 (torn write, foreign bytes) raised past
        # this handler -- UnicodeDecodeError derives from ValueError, NOT OSError
        # -- and main() has no per-file guard, so ONE damaged session aborted the
        # whole batch. Report it and let the scan continue.
        report["error"] = f"decode_failed:{exc}"
        return report
    report["bytes_before"] = len(raw.encode("utf-8"))
    report["bytes_after"] = report["bytes_before"]
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        report["error"] = f"parse_failed:{exc}"
        return report
    if not isinstance(payload, dict):
        report["error"] = "not_an_object"
        return report

    projected = project_message_payloads(payload, keys=_projection_keys(include_context))
    if projected is payload:
        return report

    rows_before = len(payload.get("messages") or [])
    rows_after = len(projected.get("messages") or [])
    if rows_before != rows_after:
        report["error"] = f"row_count_changed:{rows_before}->{rows_after}"
        return report

    encoded = _encode_like(raw, projected)
    report["changed"] = True
    report["bytes_after"] = len(encoded.encode("utf-8"))
    if not apply:
        return report

    # Conflict check: refuse to replace a file a live writer touched since we
    # read it. The tool does not hold the per-session mutation lock, so this is
    # best-effort protection, not a lock -- run it with writers stopped.
    signature_before = _stat_signature(path)
    backup = path.with_suffix(path.suffix + ".preimage-migration.bak")
    if not backup.exists():
        try:
            backup.write_text(raw, encoding="utf-8")
        except OSError as exc:
            # This ran outside the write handler below, so a full disk or a
            # permissions error here escaped main() and killed the whole batch
            # instead of failing this one file.
            report["error"] = f"backup_failed:{exc}"
            report["changed"] = False
            return report
    if _stat_signature(path) != signature_before:
        report["error"] = "concurrent_write_detected"
        report["changed"] = False
        return report
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        report["error"] = f"write_failed:{exc}"
        report["changed"] = False
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--session-dir", type=Path, default=None,
                        help="session directory (default: $HERMES_WEBUI_STATE_DIR/sessions or ~/.hermes/webui/sessions)")
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    parser.add_argument("--limit", type=int, default=0, help="stop after N files (0 = all)")
    parser.add_argument("--include-context", action="store_true",
                        help="also rewrite context_messages (changes model replay fidelity)")
    args = parser.parse_args(argv)

    session_dir = (args.session_dir or _default_session_dir()).expanduser()
    if not session_dir.is_dir():
        print(f"session dir not found: {session_dir}", file=sys.stderr)
        return 2

    print(f"override version: {PERSISTENCE_OVERRIDE_VERSION}")
    print(f"session dir:      {session_dir}")
    print(f"mode:             {'APPLY' if args.apply else 'dry run'}")
    print()

    files = list(_iter_session_files(session_dir))
    if args.limit:
        files = files[: args.limit]

    changed = 0
    saved = 0
    errors = 0
    for path in files:
        report = migrate_file(path, apply=args.apply, include_context=args.include_context)
        if report["error"]:
            errors += 1
            print(f"  SKIP  {report['file']}: {report['error']}")
            continue
        if not report["changed"]:
            continue
        changed += 1
        delta = report["bytes_before"] - report["bytes_after"]
        saved += delta
        print(f"  {'REWRITE' if args.apply else 'WOULD '} {report['file']}: "
              f"{report['bytes_before']} -> {report['bytes_after']} bytes (-{delta})")

    print()
    print(f"files scanned: {len(files)}  changed: {changed}  errors: {errors}")
    print(f"bytes reclaimed: {saved}")
    if not args.apply and changed:
        print("re-run with --apply to write these changes (backups are kept)")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
