"""Web Push notifications for the WebUI PWA.

Local `Notification`/`reg.showNotification()` calls (see messages.js
`_showPwaNotification`) only fire while the page or its service worker is
actively alive -- on iOS specifically, a backgrounded/locked PWA gets no
wake-up at all, so a time-sensitive prompt (an execute_code approval, most
notably) can sit unseen until it times out no matter how long the timeout is.
Web Push (RFC 8030 + VAPID) is the only mechanism that can wake a fully
closed PWA, because delivery goes through the OS's own push service instead
of the page's JS.

OPTIONAL, same convention as edge-tts/psutil/docx in requirements.txt: needs
`pywebpush` (`pip install pywebpush`). Absent, every function here degrades
to a no-op -- approval flow and the existing foreground notifications are
completely unaffected either way. VAPID keypair generation only needs
`cryptography`, already a hard requirement, so the public-key endpoint works
even without pywebpush installed; only the actual send is gated on it.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

try:
    from pywebpush import webpush, WebPushException
    _HAVE_PYWEBPUSH = True
except ImportError:
    _HAVE_PYWEBPUSH = False

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

_vapid_lock = threading.Lock()
_subs_lock = threading.Lock()


def _webui_home() -> Path:
    """Install-wide state dir -- deliberately NOT get_active_hermes_home().

    That helper resolves per-request against whichever profile is currently
    active (issue #798 per-request TLS context) -- fine for profile-owned
    data, wrong here. A push subscription belongs to the physical
    device/browser install, not to whatever profile happened to be active
    in the tab when the user clicked "Enable notifications"; a subscription
    filed under one profile's dir would be invisible to send_push_to_all()
    when a *different* profile's session is the one raising the approval
    that's supposed to wake the phone. Same fix api/extensions.py already
    uses for its own profile-independent state.
    """
    try:
        from api.config import STATE_DIR
        return Path(STATE_DIR)
    except Exception:
        return Path(os.getenv("HERMES_WEBUI_STATE_DIR", str(Path.home() / ".hermes" / "webui"))).expanduser()


def _vapid_private_key_path() -> Path:
    return _webui_home() / "push_vapid_private_key.pem"


def _subscriptions_path() -> Path:
    return _webui_home() / "push_subscriptions.json"


def _generate_vapid_keypair_pem() -> bytes:
    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _public_b64url_from_pem(private_pem: bytes) -> str:
    private_key = serialization.load_pem_private_key(private_pem, password=None)
    public_raw = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return base64.urlsafe_b64encode(public_raw).rstrip(b"=").decode("ascii")


def ensure_vapid_keys() -> dict | None:
    """Return {"private_key_path": str, "public_key": b64url str}.

    Generates and persists a keypair on first call (0600, matching the
    other secret-bearing files this profile writes). A stable keypair is
    required -- the browser's PushSubscription is bound to the public key
    it subscribed with, so rotating keys silently would orphan every
    existing subscription.
    """
    path = _vapid_private_key_path()
    if not path.exists():
        with _vapid_lock:
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                pem = _generate_vapid_keypair_pem()
                path.write_bytes(pem)
                try:
                    path.chmod(0o600)
                except Exception:
                    pass
    try:
        pem = path.read_bytes()
        return {"private_key_path": str(path), "public_key": _public_b64url_from_pem(pem)}
    except Exception:
        logger.warning("Failed to load/derive VAPID keys", exc_info=True)
        return None


def _load_subscriptions() -> list:
    p = _subscriptions_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_subscriptions(subs: list) -> None:
    p = _subscriptions_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(subs, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        p.chmod(0o600)
    except Exception:
        pass


def add_subscription(subscription: dict) -> None:
    """Store a PushSubscription.toJSON() payload, de-duped by endpoint."""
    endpoint = str((subscription or {}).get("endpoint") or "").strip()
    if not endpoint:
        return
    with _subs_lock:
        subs = [s for s in _load_subscriptions() if s.get("endpoint") != endpoint]
        subs.append(subscription)
        _save_subscriptions(subs)


def remove_subscription(endpoint: str) -> None:
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return
    with _subs_lock:
        subs = [s for s in _load_subscriptions() if s.get("endpoint") != endpoint]
        _save_subscriptions(subs)


def _remove_subscriptions(endpoints: list) -> None:
    if not endpoints:
        return
    dead = set(endpoints)
    with _subs_lock:
        subs = [s for s in _load_subscriptions() if s.get("endpoint") not in dead]
        _save_subscriptions(subs)


def send_push_to_all(title: str, body: str, *, url: str = "./", tag: str | None = None) -> None:
    """Best-effort push to every stored subscription. Never raises.

    A subscription answering 404/410 (Gone) means the browser/OS dropped it
    -- pywebpush surfaces that as WebPushException with response.status_code
    set, and it's the standard signal to stop sending to that endpoint
    rather than retry it forever.
    """
    if not _HAVE_PYWEBPUSH:
        return
    subs = _load_subscriptions()
    if not subs:
        return
    keys = ensure_vapid_keys()
    if not keys:
        return
    payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag or "hermes-push"})
    stale: list[str] = []
    for sub in subs:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=keys["private_key_path"],
                # Apple's push service (web.push.apple.com) is documented to be
                # stricter than other push services about `sub`: it must be a
                # genuine, reachable contact per RFC 8292's intent (so the push
                # service operator can reach the sender about abuse), not just
                # syntactically mailto-shaped. A fake, non-resolvable domain
                # (e.g. the previous "hermes.local") gets rejected as
                # BadJwtToken even though the JWT is otherwise well-formed.
                vapid_claims={"sub": "mailto:dgzap111@gmail.com"},
            )
        except WebPushException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in (404, 410):
                stale.append(sub.get("endpoint"))
            else:
                logger.warning("Push send failed (%s): %s", status, exc)
        except Exception:
            logger.warning("Push send failed", exc_info=True)
    if stale:
        _remove_subscriptions(stale)
