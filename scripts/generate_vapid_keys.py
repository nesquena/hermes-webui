#!/usr/bin/env python3
"""Generate VAPID keys for Hermes WebUI Web Push.

Writes ``<state-dir>/webui_vapid.json`` (mode 0600). The private key is never
printed. Requires ``pip install pywebpush``.

    python scripts/generate_vapid_keys.py --subject mailto:you@example.com
    python scripts/generate_vapid_keys.py --force   # rotate (drops trust in old subscriptions)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path


def _default_state_dir() -> str:
    """Resolve the state dir exactly as the server does (api/config.py):
    $HERMES_WEBUI_STATE_DIR, else <HERMES_HOME or platform default>/webui."""
    repo_root = str(Path(__file__).resolve().parent.parent)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from api.config import STATE_DIR

    return str(STATE_DIR)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state-dir", default=None,
                    help="WebUI state dir (default: same as the server: "
                         "$HERMES_WEBUI_STATE_DIR, else <HERMES_HOME>/webui)")
    ap.add_argument("--subject", default=os.getenv("HERMES_WEBUI_VAPID_SUBJECT"),
                    help="VAPID subject, e.g. mailto:you@example.com")
    ap.add_argument("--force", action="store_true", help="overwrite existing keys")
    args = ap.parse_args(argv)
    if not args.subject:
        ap.error("--subject is required (or set HERMES_WEBUI_VAPID_SUBJECT)")
    if not args.state_dir:
        args.state_dir = _default_state_dir()
    try:
        from cryptography.hazmat.primitives import serialization
        from py_vapid import Vapid
    except ImportError:
        print("pywebpush is required: pip install pywebpush", file=sys.stderr)
        return 2
    state = Path(args.state_dir).expanduser()
    state.mkdir(parents=True, exist_ok=True)
    target = state / "webui_vapid.json"
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to rotate)", file=sys.stderr)
        return 1
    vapid = Vapid()
    vapid.generate_keys()
    public = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    number = vapid.private_key.private_numbers().private_value
    private = number.to_bytes(32, "big")
    payload = {"public_key": _b64url(public), "private_key": _b64url(private),
               "subject": args.subject}
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
        fh.write("\n")
    os.chmod(target, 0o600)
    print(f"wrote {target} (public key {payload['public_key']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
