#!/usr/bin/env python3
"""Rewrite existing WebUI session files to drop persisted inline image bytes.

The save-boundary projection (api/session_image_persistence_override.py) only
bounds NEW writes. Sessions written before PERSISTENCE_OVERRIDE_VERSION 1.0.0 can
still carry megabytes of ``data:image/...;base64,...`` in ``messages`` and
``context_messages``, which is what made history requests time out.

This tool is opt-in and conservative:

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
    state = os.getenv("HERMES_WEBUI_STATE_DIR", "").strip()
    base = Path(state).expanduser() if state else Path.home() / ".hermes" / "webui"
    return base / "sessions"


def _iter_session_files(session_dir: Path):
    for path in sorted(session_dir.glob("*.json")):
        if path.name.startswith(("_", ".")) or path.name.endswith(".bak"):
            continue
        yield path


def _projection_keys(include_context: bool):
    return ("messages", "context_messages") if include_context else ("messages",)


def migrate_file(path: Path, *, apply: bool, include_context: bool = False) -> dict:
    """Rewrite one session file. Returns a per-file report dict."""
    report = {"file": path.name, "changed": False, "error": None,
              "bytes_before": 0, "bytes_after": 0}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        report["error"] = f"read_failed:{exc}"
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

    encoded = json.dumps(projected, ensure_ascii=False, indent=2)
    try:
        encoded.encode("utf-8")
    except UnicodeEncodeError:
        encoded = json.dumps(projected, ensure_ascii=True, indent=2)
    report["changed"] = True
    report["bytes_after"] = len(encoded.encode("utf-8"))
    if not apply:
        return report

    backup = path.with_suffix(path.suffix + ".preimage-migration.bak")
    if not backup.exists():
        backup.write_text(raw, encoding="utf-8")
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
