"""Passkey/WebAuthn helpers for Hermes WebUI.

Default-off: passkeys are only advertised after an authenticated user registers
one from Settings. Password auth remains the bootstrap/recovery mechanism.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from api.config import STATE_DIR

try:  # optional at import-time; endpoints return a clear error if unavailable
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
except Exception:  # pragma: no cover - exercised by source tests instead
    InvalidSignature = Exception  # type: ignore[assignment]
    hashes = serialization = ec = None  # type: ignore[assignment]

_CREDENTIALS_FILE = STATE_DIR / "passkeys.json"
_CHALLENGES_FILE = STATE_DIR / ".passkey_challenges.json"
_CHALLENGE_TTL = 300
_RP_NAME = "Hermes WebUI"


class PasskeyError(ValueError):
    """Raised for user-correctable WebAuthn failures."""


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(value: str | bytes) -> bytes:
    if isinstance(value, bytes):
        value = value.decode("ascii")
    value = str(value).strip()
    value += "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value.encode("ascii"))


def _json_load(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default
    return default


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_credentials() -> list[dict[str, Any]]:
    data = _json_load(_CREDENTIALS_FILE, [])
    if not isinstance(data, list):
        return []
    return [c for c in data if isinstance(c, dict) and isinstance(c.get("id"), str)]


def _save_credentials(creds: list[dict[str, Any]]) -> None:
    _atomic_write_json(_CREDENTIALS_FILE, creds)


def registered_credentials() -> list[dict[str, Any]]:
    """Return public credential metadata only; never expose public keys."""
    out = []
    for c in _load_credentials():
        out.append({
            "id": c.get("id"),
            "label": c.get("label") or "Passkey",
            "created_at": c.get("created_at"),
            "last_used_at": c.get("last_used_at"),
            "sign_count": c.get("sign_count", 0),
        })
    return out


def passkeys_available() -> bool:
    return bool(_load_credentials())


def _load_challenges() -> dict[str, dict[str, Any]]:
    raw = _json_load(_CHALLENGES_FILE, {})
    if not isinstance(raw, dict):
        return {}
    now = time.time()
    clean = {
        k: v for k, v in raw.items()
        if isinstance(k, str) and isinstance(v, dict) and now - float(v.get("ts", 0)) < _CHALLENGE_TTL
    }
    if clean != raw:
        _atomic_write_json(_CHALLENGES_FILE, clean)
    return clean


def _store_challenge(challenge: str, kind: str, rp_id: str, origin: str) -> None:
    data = _load_challenges()
    data[challenge] = {"kind": kind, "rp_id": rp_id, "origin": origin, "ts": time.time()}
    _atomic_write_json(_CHALLENGES_FILE, data)


def _consume_challenge(challenge: str, kind: str) -> dict[str, Any]:
    data = _load_challenges()
    entry = data.pop(challenge, None)
    _atomic_write_json(_CHALLENGES_FILE, data)
    if not entry or entry.get("kind") != kind:
        raise PasskeyError("Passkey challenge expired. Try again.")
    return entry


def _host_without_port(host: str) -> str:
    host = (host or "localhost").strip().split(",", 1)[0]
    if host.startswith("[") and "]" in host:
        return host[1:host.index("]")]
    return host.rsplit(":", 1)[0] if ":" in host else host


def rp_context(handler) -> tuple[str, str]:
    host = _host_without_port(handler.headers.get("Host", "localhost"))
    proto = handler.headers.get("X-Forwarded-Proto", "").split(",", 1)[0].strip().lower()
    if proto not in {"http", "https"}:
        try:
            from api.auth import _is_secure_context
            proto = "https" if _is_secure_context(handler) else "http"
        except AttributeError:
            proto = "http"
    return host, f"{proto}://{handler.headers.get('Host', host)}"


def registration_options(handler) -> dict[str, Any]:
    rp_id, _origin = rp_context(handler)
    challenge = _b64u(secrets.token_bytes(32))
    _store_challenge(challenge, "register", rp_id, _origin)
    return {
        "challenge": challenge,
        "rp": {"name": _RP_NAME, "id": rp_id},
        "user": {"id": _b64u(hashlib.sha256(rp_id.encode()).digest()[:16]), "name": "Hermes WebUI", "displayName": "Hermes WebUI"},
        "pubKeyCredParams": [{"type": "public-key", "alg": -7}],
        "authenticatorSelection": {"residentKey": "preferred", "userVerification": "preferred"},
        "timeout": 60000,
        "attestation": "none",
        "excludeCredentials": [{"type": "public-key", "id": c["id"]} for c in registered_credentials()],
    }


def authentication_options(handler) -> dict[str, Any]:
    creds = registered_credentials()
    if not creds:
        raise PasskeyError("No passkeys are registered.")
    rp_id, origin = rp_context(handler)
    challenge = _b64u(secrets.token_bytes(32))
    _store_challenge(challenge, "login", rp_id, origin)
    return {
        "challenge": challenge,
        "rpId": rp_id,
        "allowCredentials": [{"type": "public-key", "id": c["id"]} for c in creds],
        "timeout": 60000,
        "userVerification": "preferred",
    }


@dataclass
class _Cbor:
    data: bytes
    pos: int = 0

    def read(self, n: int) -> bytes:
        if self.pos + n > len(self.data):
            raise PasskeyError("Malformed CBOR data")
        out = self.data[self.pos:self.pos + n]
        self.pos += n
        return out

    def item(self) -> Any:
        initial = self.read(1)[0]
        major, addl = initial >> 5, initial & 0x1F
        val = self._val(addl)
        if major == 0:
            return val
        if major == 1:
            return -1 - val
        if major == 2:
            return self.read(val)
        if major == 3:
            return self.read(val).decode("utf-8")
        if major == 4:
            return [self.item() for _ in range(val)]
        if major == 5:
            return {self.item(): self.item() for _ in range(val)}
        if major == 7:
            if val == 20:
                return False
            if val == 21:
                return True
            if val == 22:
                return None
        raise PasskeyError("Unsupported CBOR data")

    def _val(self, addl: int) -> int:
        if addl < 24:
            return addl
        if addl == 24:
            return self.read(1)[0]
        if addl == 25:
            return int.from_bytes(self.read(2), "big")
        if addl == 26:
            return int.from_bytes(self.read(4), "big")
        if addl == 27:
            return int.from_bytes(self.read(8), "big")
        raise PasskeyError("Indefinite CBOR values are not supported")


def _cbor_loads(data: bytes) -> Any:
    parser = _Cbor(data)
    value = parser.item()
    if parser.pos != len(data):
        raise PasskeyError("Trailing CBOR data")
    return value


def _client_data(encoded: str, expected_type: str, challenge_kind: str) -> tuple[dict[str, Any], dict[str, Any], bytes]:
    raw = _b64u_decode(encoded)
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise PasskeyError("Malformed client data") from exc
    if data.get("type") != expected_type:
        raise PasskeyError("Unexpected passkey response type")
    challenge = data.get("challenge")
    if not isinstance(challenge, str):
        raise PasskeyError("Missing passkey challenge")
    entry = _consume_challenge(challenge, challenge_kind)
    if data.get("origin") != entry.get("origin"):
        raise PasskeyError("Passkey origin mismatch")
    return data, entry, raw


def _parse_auth_data(auth_data: bytes, rp_id: str) -> dict[str, Any]:
    if len(auth_data) < 37:
        raise PasskeyError("Malformed authenticator data")
    rp_hash = auth_data[:32]
    expected = hashlib.sha256(rp_id.encode("idna")).digest()
    if not hmac.compare_digest(rp_hash, expected):
        raise PasskeyError("Passkey RP ID mismatch")
    flags = auth_data[32]
    if not (flags & 0x01):
        raise PasskeyError("Passkey user presence was not verified")
    sign_count = int.from_bytes(auth_data[33:37], "big")
    return {"flags": flags, "sign_count": sign_count, "rest": auth_data[37:]}


def _public_key_from_cose(cose: dict[Any, Any]):
    if ec is None or serialization is None:
        raise PasskeyError("Passkey support requires the cryptography package")
    alg = cose.get(3)
    kty = cose.get(1)
    crv = cose.get(-1)
    x = cose.get(-2)
    y = cose.get(-3)
    if alg != -7 or kty != 2 or crv != 1 or not isinstance(x, bytes) or not isinstance(y, bytes):
        raise PasskeyError("Only ES256 passkeys are supported")
    numbers = ec.EllipticCurvePublicNumbers(int.from_bytes(x, "big"), int.from_bytes(y, "big"), ec.SECP256R1())
    return numbers.public_key()


def finish_registration(payload: dict[str, Any], handler) -> dict[str, Any]:
    response = payload.get("response") or {}
    _client, entry, _client_raw = _client_data(response.get("clientDataJSON", ""), "webauthn.create", "register")
    att_obj = _cbor_loads(_b64u_decode(response.get("attestationObject", "")))
    if not isinstance(att_obj, dict) or not isinstance(att_obj.get("authData"), bytes):
        raise PasskeyError("Malformed attestation object")
    parsed = _parse_auth_data(att_obj["authData"], entry["rp_id"])
    if not (parsed["flags"] & 0x40):
        raise PasskeyError("Passkey credential data missing")
    rest = parsed["rest"]
    if len(rest) < 18:
        raise PasskeyError("Malformed credential data")
    cred_len = int.from_bytes(rest[16:18], "big")
    credential_id = rest[18:18 + cred_len]
    cose_bytes = rest[18 + cred_len:]
    cose_key = _cbor_loads(cose_bytes)
    public_key = _public_key_from_cose(cose_key)
    pem = public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    cred_id = _b64u(credential_id)
    label = str(payload.get("label") or "Passkey").strip()[:80] or "Passkey"
    creds = [c for c in _load_credentials() if c.get("id") != cred_id]
    creds.append({
        "id": cred_id,
        "label": label,
        "public_key_pem": pem,
        "sign_count": parsed["sign_count"],
        "created_at": time.time(),
        "last_used_at": None,
    })
    _save_credentials(creds)
    return {"ok": True, "credential": {"id": cred_id, "label": label}}


def finish_login(payload: dict[str, Any], handler) -> dict[str, Any]:
    if serialization is None or hashes is None:
        raise PasskeyError("Passkey support requires the cryptography package")
    response = payload.get("response") or {}
    cred_id = payload.get("id") or payload.get("rawId")
    if not isinstance(cred_id, str):
        raise PasskeyError("Missing passkey credential id")
    creds = _load_credentials()
    idx = next((i for i, c in enumerate(creds) if c.get("id") == cred_id), -1)
    if idx < 0:
        raise PasskeyError("Unknown passkey")
    _client, entry, client_raw = _client_data(response.get("clientDataJSON", ""), "webauthn.get", "login")
    auth_data = _b64u_decode(response.get("authenticatorData", ""))
    parsed = _parse_auth_data(auth_data, entry["rp_id"])
    signature = _b64u_decode(response.get("signature", ""))
    public_key = serialization.load_pem_public_key(str(creds[idx].get("public_key_pem", "")).encode("ascii"))
    signed = auth_data + hashlib.sha256(client_raw).digest()
    try:
        public_key.verify(signature, signed, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise PasskeyError("Passkey signature verification failed") from exc
    old_count = int(creds[idx].get("sign_count") or 0)
    if parsed["sign_count"] and old_count and parsed["sign_count"] <= old_count:
        raise PasskeyError("Passkey sign counter did not advance")
    creds[idx]["sign_count"] = parsed["sign_count"] or old_count
    creds[idx]["last_used_at"] = time.time()
    _save_credentials(creds)
    return {"ok": True, "credential_id": cred_id}


def delete_credential(credential_id: str) -> dict[str, Any]:
    creds = _load_credentials()
    kept = [c for c in creds if c.get("id") != credential_id]
    if len(kept) == len(creds):
        raise PasskeyError("Passkey not found")
    _save_credentials(kept)
    return {"ok": True, "credentials": registered_credentials()}


def clear_credentials() -> None:
    """Remove all registered passkeys when the user disables all auth."""
    if _CREDENTIALS_FILE.exists():
        _save_credentials([])
