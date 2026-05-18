"""WebAuthn passkey support for the single-user WebUI auth gate."""
from __future__ import annotations

import base64
import json
import os
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from api.config import STATE_DIR, load_settings


_PASSKEYS_FILE = STATE_DIR / "passkeys.json"
_CHALLENGE_TTL_SECONDS = 300
_LOCK = threading.Lock()
_CHALLENGES: dict[str, dict[str, Any]] = {}


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(value: str) -> bytes:
    value = (value or "").strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _load_webauthn():
    try:
        from webauthn import (  # type: ignore
            base64url_to_bytes,
            generate_authentication_options,
            generate_registration_options,
            options_to_json,
            verify_authentication_response,
            verify_registration_response,
        )
        from webauthn.helpers.structs import (  # type: ignore
            AuthenticatorSelectionCriteria,
            PublicKeyCredentialDescriptor,
            ResidentKeyRequirement,
            UserVerificationRequirement,
        )
    except ImportError as exc:  # pragma: no cover - exercised when dependency missing
        raise RuntimeError(
            "Passkey support requires the 'webauthn' Python package. "
            "Install project requirements and restart Hermes WebUI."
        ) from exc
    return {
        "base64url_to_bytes": base64url_to_bytes,
        "generate_authentication_options": generate_authentication_options,
        "generate_registration_options": generate_registration_options,
        "options_to_json": options_to_json,
        "verify_authentication_response": verify_authentication_response,
        "verify_registration_response": verify_registration_response,
        "AuthenticatorSelectionCriteria": AuthenticatorSelectionCriteria,
        "PublicKeyCredentialDescriptor": PublicKeyCredentialDescriptor,
        "ResidentKeyRequirement": ResidentKeyRequirement,
        "UserVerificationRequirement": UserVerificationRequirement,
    }


def _default_store() -> dict[str, Any]:
    return {
        "version": 1,
        "user_id": _b64e(secrets.token_bytes(32)),
        "credentials": [],
    }


def _load_store() -> dict[str, Any]:
    try:
        if _PASSKEYS_FILE.exists():
            data = json.loads(_PASSKEYS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("user_id", _b64e(secrets.token_bytes(32)))
                creds = data.get("credentials")
                data["credentials"] = creds if isinstance(creds, list) else []
                return data
    except Exception:
        pass
    return _default_store()


def _save_store(store: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, suffix=".passkeys.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _PASSKEYS_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _credential_count_unlocked(store: dict[str, Any]) -> int:
    return sum(1 for c in store.get("credentials", []) if isinstance(c, dict) and c.get("id"))


def credential_count() -> int:
    with _LOCK:
        return _credential_count_unlocked(_load_store())


def passkeys_available() -> bool:
    return credential_count() > 0


def _host_without_port(host: str) -> str:
    host = (host or "localhost").strip()
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    if ":" in host and host.count(":") == 1:
        return host.split(":", 1)[0]
    return host


def _rp_context(handler) -> tuple[str, str, str]:
    rp_id = os.getenv("HERMES_WEBUI_PASSKEY_RP_ID", "").strip()
    origin = os.getenv("HERMES_WEBUI_PASSKEY_ORIGIN", "").strip().rstrip("/")
    host_header = handler.headers.get("X-Forwarded-Host") or handler.headers.get("Host") or "localhost"
    host = _host_without_port(host_header)
    if not rp_id:
        rp_id = host
    if not origin:
        proto = handler.headers.get("X-Forwarded-Proto", "").strip().lower()
        if proto not in ("http", "https"):
            try:
                proto = "https" if getattr(handler.request, "getpeercert", None) is not None else "http"
            except Exception:
                proto = "http"
        origin = f"{proto}://{host_header}"
    return rp_id, origin, load_settings().get("bot_name") or "Hermes"


def _challenge_key(kind: str) -> str:
    return f"{kind}:{secrets.token_urlsafe(18)}"


def _remember_challenge(kind: str, challenge: bytes, rp_id: str, origin: str) -> str:
    key = _challenge_key(kind)
    now = time.time()
    with _LOCK:
        expired = [k for k, v in _CHALLENGES.items() if now > float(v.get("expires_at", 0))]
        for k in expired:
            _CHALLENGES.pop(k, None)
        _CHALLENGES[key] = {
            "kind": kind,
            "challenge": challenge,
            "rp_id": rp_id,
            "origin": origin,
            "expires_at": now + _CHALLENGE_TTL_SECONDS,
        }
    return key


def _take_challenge(kind: str, key: str) -> dict[str, Any]:
    with _LOCK:
        item = _CHALLENGES.pop(key or "", None)
    if not item or item.get("kind") != kind or time.time() > float(item.get("expires_at", 0)):
        raise ValueError("Passkey challenge expired. Try again.")
    return item


def registration_options(handler) -> dict[str, Any]:
    webauthn = _load_webauthn()
    rp_id, _origin, rp_name = _rp_context(handler)
    with _LOCK:
        store = _load_store()
        if not _PASSKEYS_FILE.exists():
            _save_store(store)
        exclude = [
            webauthn["PublicKeyCredentialDescriptor"](id=_b64d(str(c.get("id", ""))))
            for c in store.get("credentials", [])
            if isinstance(c, dict) and c.get("id")
        ]
        user_id = _b64d(str(store.get("user_id") or ""))
    options = webauthn["generate_registration_options"](
        rp_id=rp_id,
        rp_name=rp_name,
        user_id=user_id,
        user_name="hermes",
        user_display_name=rp_name,
        exclude_credentials=exclude,
        authenticator_selection=webauthn["AuthenticatorSelectionCriteria"](
            resident_key=webauthn["ResidentKeyRequirement"].PREFERRED,
            user_verification=webauthn["UserVerificationRequirement"].PREFERRED,
        ),
    )
    challenge_id = _remember_challenge("registration", options.challenge, rp_id, _origin)
    payload = json.loads(webauthn["options_to_json"](options))
    payload["challenge_id"] = challenge_id
    return payload


def verify_registration(handler, credential: dict[str, Any], challenge_id: str, nickname: str = "") -> dict[str, Any]:
    webauthn = _load_webauthn()
    challenge = _take_challenge("registration", challenge_id)
    verification = webauthn["verify_registration_response"](
        credential=credential,
        expected_challenge=challenge["challenge"],
        expected_rp_id=challenge["rp_id"],
        expected_origin=challenge["origin"],
        require_user_verification=False,
    )
    transports = []
    try:
        transports = list((credential.get("response") or {}).get("transports") or [])
    except Exception:
        transports = []
    record = {
        "id": _b64e(verification.credential_id),
        "public_key": _b64e(verification.credential_public_key),
        "sign_count": int(verification.sign_count or 0),
        "device_type": str(getattr(verification, "credential_device_type", "") or ""),
        "backed_up": bool(getattr(verification, "credential_backed_up", False)),
        "transports": [str(t) for t in transports if isinstance(t, str)],
        "nickname": (nickname or "Passkey").strip()[:80],
        "created_at": int(time.time()),
        "last_used_at": None,
    }
    with _LOCK:
        store = _load_store()
        creds = [c for c in store.get("credentials", []) if isinstance(c, dict) and c.get("id") != record["id"]]
        creds.append(record)
        store["credentials"] = creds
        _save_store(store)
        count = _credential_count_unlocked(store)
    return {"ok": True, "credential_count": count}


def authentication_options(handler) -> dict[str, Any]:
    webauthn = _load_webauthn()
    rp_id, origin, _rp_name = _rp_context(handler)
    with _LOCK:
        store = _load_store()
        credentials = [
            c for c in store.get("credentials", [])
            if isinstance(c, dict) and c.get("id") and c.get("public_key")
        ]
    if not credentials:
        raise ValueError("No passkeys are registered for this Hermes WebUI.")
    allow_credentials = [
        webauthn["PublicKeyCredentialDescriptor"](id=_b64d(str(c["id"])))
        for c in credentials
    ]
    options = webauthn["generate_authentication_options"](
        rp_id=rp_id,
        allow_credentials=allow_credentials,
        user_verification=webauthn["UserVerificationRequirement"].PREFERRED,
    )
    challenge_id = _remember_challenge("authentication", options.challenge, rp_id, origin)
    payload = json.loads(webauthn["options_to_json"](options))
    payload["challenge_id"] = challenge_id
    return payload


def verify_authentication(handler, credential: dict[str, Any], challenge_id: str) -> dict[str, Any]:
    webauthn = _load_webauthn()
    challenge = _take_challenge("authentication", challenge_id)
    credential_id = str(credential.get("id") or "")
    with _LOCK:
        store = _load_store()
        credentials = [
            c for c in store.get("credentials", [])
            if isinstance(c, dict) and c.get("id") and c.get("public_key")
        ]
        record = next((c for c in credentials if c.get("id") == credential_id), None)
    if not record:
        raise ValueError("Unknown passkey.")
    verification = webauthn["verify_authentication_response"](
        credential=credential,
        expected_challenge=challenge["challenge"],
        expected_rp_id=challenge["rp_id"],
        expected_origin=challenge["origin"],
        credential_public_key=_b64d(str(record["public_key"])),
        credential_current_sign_count=int(record.get("sign_count") or 0),
        require_user_verification=False,
    )
    with _LOCK:
        store = _load_store()
        for item in store.get("credentials", []):
            if isinstance(item, dict) and item.get("id") == credential_id:
                item["sign_count"] = int(verification.new_sign_count or 0)
                item["last_used_at"] = int(time.time())
                item["device_type"] = str(getattr(verification, "credential_device_type", item.get("device_type", "")) or "")
                item["backed_up"] = bool(getattr(verification, "credential_backed_up", item.get("backed_up", False)))
                break
        _save_store(store)
    return {"ok": True}
