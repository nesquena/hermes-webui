import copy
import base64
import hashlib
import ipaddress
import json
import logging
import math
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

from api.config import get_config

logger = logging.getLogger(__name__)

_DEFAULT_SCOPES = ("openid", "profile", "email")
_PENDING_TTL_SECONDS = 600
_MAX_PENDING_FLOWS = 128
_CLOCK_SKEW_SECONDS = 60
_CACHE_TTL_SECONDS = 300

_pending_lock = threading.Lock()
_pending_flows: dict[str, dict[str, Any]] = {}

_discovery_lock = threading.Lock()
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}

_jwks_lock = threading.Lock()
_jwks_cache: dict[str, tuple[float, dict[str, Any]]] = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class OIDCConfigError(Exception):
    pass


class OIDCAuthError(Exception):
    def __init__(self, message: str, *, status_code: int = 401):
        super().__init__(message)
        self.status_code = status_code


def is_oidc_enabled() -> bool:
    cfg = _resolve_oidc_config()
    return bool(
        cfg.get("issuer")
        and cfg.get("client_id")
        and cfg.get("allow_claim")
        and cfg.get("allow_values")
    )


def build_authorization_redirect(
    request_base_url: str,
    next_path: str | None = None,
) -> str:
    cfg = _require_oidc_config()
    discovery = _get_discovery_document(cfg["issuer"])
    authorization_endpoint = str(discovery.get("authorization_endpoint") or "").strip()
    if not authorization_endpoint:
        raise OIDCConfigError("OIDC discovery document is missing authorization_endpoint")
    redirect_uri = _resolve_redirect_uri(cfg, request_base_url)
    state = secrets.token_urlsafe(24)
    nonce = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64u(hashlib.sha256(verifier.encode("ascii")).digest())
    _store_pending_flow(
        state,
        {
            "created_at": time.time(),
            "nonce": nonce,
            "code_verifier": verifier,
            "next_path": _safe_next_path(next_path),
        },
    )
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri,
        "scope": " ".join(cfg["scopes"]),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return authorization_endpoint + "?" + urllib.parse.urlencode(params)


def complete_authorization_code_flow(
    request_base_url: str,
    state: str,
    code: str,
) -> dict[str, Any]:
    cfg = _require_oidc_config()
    pending = _consume_pending_flow(state)
    if pending is None:
        raise OIDCAuthError("Invalid OIDC state", status_code=401)
    discovery = _get_discovery_document(cfg["issuer"])
    discovery_issuer = str(discovery.get("issuer") or "").strip()
    if discovery_issuer and discovery_issuer != cfg["issuer"]:
        raise OIDCAuthError("OIDC discovery issuer did not match the configured issuer", status_code=502)
    token_endpoint = str(discovery.get("token_endpoint") or "").strip()
    if not token_endpoint:
        raise OIDCConfigError("OIDC discovery document is missing token_endpoint")
    redirect_uri = _resolve_redirect_uri(cfg, request_base_url)
    token_response = _post_form_json(
        token_endpoint,
        {
            "grant_type": "authorization_code",
            "client_id": cfg["client_id"],
            "code": code,
            "code_verifier": pending["code_verifier"],
            "redirect_uri": redirect_uri,
            **({"client_secret": cfg["client_secret"]} if cfg.get("client_secret") else {}),
        },
    )
    id_token = str(token_response.get("id_token") or "").strip()
    if not id_token:
        raise OIDCAuthError("OIDC token response did not include an id_token", status_code=502)
    claims = _validate_id_token(
        id_token,
        client_id=cfg["client_id"],
        issuer=cfg["issuer"],
        nonce=pending["nonce"],
        jwks_uri=str(discovery.get("jwks_uri") or "").strip(),
    )
    _enforce_allowlist(
        claims,
        allow_claim=cfg.get("allow_claim"),
        allow_values=cfg.get("allow_values") or [],
    )
    return {
        "next_path": pending["next_path"],
        "subject": str(claims.get("sub") or ""),
        "email": str(claims.get("email") or ""),
        "claims": claims,
    }


def _resolve_oidc_config() -> dict[str, Any]:
    raw = {}
    try:
        cfg = get_config()
        value = cfg.get("webui_oidc") if isinstance(cfg, dict) else None
        if isinstance(value, dict):
            raw.update(value)
    except Exception:
        logger.debug("Failed to read webui_oidc config", exc_info=True)

    def pick(name: str, env_name: str) -> Any:
        env_value = os.getenv(env_name)
        return env_value if env_value is not None else raw.get(name)

    scopes = _normalize_scopes(pick("scopes", "HERMES_WEBUI_OIDC_SCOPES"))
    allow_values = _normalize_allow_values(
        pick("allow_values", "HERMES_WEBUI_OIDC_ALLOW_VALUES")
    )
    allow_private_raw = pick(
        "allow_private_endpoints", "HERMES_WEBUI_OIDC_ALLOW_PRIVATE_ENDPOINTS"
    )
    allow_private = False
    if allow_private_raw is not None:
        if isinstance(allow_private_raw, bool):
            allow_private = allow_private_raw
        else:
            allow_private = str(allow_private_raw).strip().lower() in ("1", "true", "yes")
    return {
        "issuer": str(pick("issuer", "HERMES_WEBUI_OIDC_ISSUER") or "").strip(),
        "client_id": str(pick("client_id", "HERMES_WEBUI_OIDC_CLIENT_ID") or "").strip(),
        "client_secret": str(pick("client_secret", "HERMES_WEBUI_OIDC_CLIENT_SECRET") or "").strip(),
        "redirect_uri": str(pick("redirect_uri", "HERMES_WEBUI_OIDC_REDIRECT_URI") or "").strip(),
        "scopes": scopes,
        "allow_claim": str(pick("allow_claim", "HERMES_WEBUI_OIDC_ALLOW_CLAIM") or "").strip(),
        "allow_values": allow_values,
        "allow_private_endpoints": allow_private,
    }


def _require_oidc_config() -> dict[str, Any]:
    cfg = _resolve_oidc_config()
    if not cfg.get("issuer") or not cfg.get("client_id"):
        raise OIDCConfigError("Native OIDC login is not configured")
    if not cfg.get("allow_claim") or not cfg.get("allow_values"):
        raise OIDCConfigError(
            "Native OIDC login requires webui_oidc.allow_claim and allow_values"
        )
    return cfg


def _normalize_scopes(raw: Any) -> list[str]:
    items = _normalize_text_list(raw)
    if not items:
        return list(_DEFAULT_SCOPES)
    if "openid" not in items:
        items.insert(0, "openid")
    deduped = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _normalize_allow_values(raw: Any) -> list[str]:
    """Normalize allowlist values, splitting only on commas/newlines.

    Unlike ``_normalize_text_list`` (which also splits on whitespace), this
    preserves multi-word values such as OIDC group names containing spaces
    (e.g. ``"Hermes Users"`` stays as one entry).

    RFC 6749 §3.3 requires space-delimited scope strings, so
    ``_normalize_scopes`` must keep using ``_normalize_text_list`` -- this
    function is for allow-values only.
    """
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [value for value in (str(item).strip() for item in raw) if value]
    text = str(raw).replace("\n", ",")
    return [part.strip() for part in text.split(",") if part.strip()]


def _normalize_text_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        values = [str(item).strip() for item in raw]
    else:
        text = str(raw).replace("\n", ",")
        values = []
        for comma_part in text.split(","):
            values.extend(piece.strip() for piece in comma_part.split() if piece.strip())
    return [value for value in values if value]


def _safe_next_path(raw_path: str | None) -> str:
    path = str(raw_path or "").strip()
    if not path:
        return "/"
    if path[0] != "/":
        return "/"
    if path[1:2] in {"/", "\\"}:
        return "/"
    if any(ord(ch) < 32 or ord(ch) == 127 or ch.isspace() for ch in path):
        return "/"
    return path


def _resolve_redirect_uri(cfg: dict[str, Any], request_base_url: str) -> str:
    explicit = str(cfg.get("redirect_uri") or "").strip()
    if explicit:
        return explicit
    return request_base_url.rstrip("/") + "/api/auth/oidc/callback"


def _store_pending_flow(state: str, payload: dict[str, Any]) -> None:
    now = time.time()
    with _pending_lock:
        _prune_pending_flows(now)
        _trim_pending_flows()
        _pending_flows[state] = payload


def _consume_pending_flow(state: str) -> dict[str, Any] | None:
    now = time.time()
    with _pending_lock:
        _prune_pending_flows(now)
        payload = _pending_flows.pop(state, None)
    return payload


def _prune_pending_flows(now: float) -> None:
    expired = [
        state
        for state, payload in _pending_flows.items()
        if now - float(payload.get("created_at") or 0) > _PENDING_TTL_SECONDS
    ]
    for state in expired:
        _pending_flows.pop(state, None)


def _trim_pending_flows() -> None:
    overflow = len(_pending_flows) - _MAX_PENDING_FLOWS + 1
    if overflow <= 0:
        return
    oldest = sorted(
        _pending_flows,
        key=lambda state: float(_pending_flows[state].get("created_at") or 0),
    )
    for state in oldest[:overflow]:
        _pending_flows.pop(state, None)


def _get_discovery_document(issuer: str) -> dict[str, Any]:
    discovery_url = _discovery_url_for_issuer(issuer)
    cached = _cache_get(_discovery_lock, _discovery_cache, discovery_url)
    if cached is not None:
        return cached
    data = _fetch_json(discovery_url)
    if not isinstance(data, dict):
        raise OIDCAuthError("OIDC discovery response was not a JSON object", status_code=502)
    _cache_put(_discovery_lock, _discovery_cache, discovery_url, data)
    return data


def _discovery_url_for_issuer(issuer: str) -> str:
    if issuer.endswith("/.well-known/openid-configuration"):
        return issuer
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


def _get_jwks_document(jwks_uri: str, *, force_refresh: bool = False) -> dict[str, Any]:
    if not jwks_uri:
        raise OIDCConfigError("OIDC discovery document is missing jwks_uri")
    if force_refresh:
        with _jwks_lock:
            _jwks_cache.pop(jwks_uri, None)
    else:
        cached = _cache_get(_jwks_lock, _jwks_cache, jwks_uri)
        if cached is not None:
            return cached
    data = _fetch_json(jwks_uri)
    if not isinstance(data, dict):
        raise OIDCAuthError("OIDC JWKS response was not a JSON object", status_code=502)
    _cache_put(_jwks_lock, _jwks_cache, jwks_uri, data)
    return data


def _cache_get(
    lock: threading.Lock,
    cache: dict[str, tuple[float, dict[str, Any]]],
    key: str,
) -> dict[str, Any] | None:
    now = time.time()
    with lock:
        entry = cache.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if expires_at <= now:
            cache.pop(key, None)
            return None
        return copy.deepcopy(value)


def _cache_put(
    lock: threading.Lock,
    cache: dict[str, tuple[float, dict[str, Any]]],
    key: str,
    value: dict[str, Any],
) -> None:
    with lock:
        cache[key] = (time.time() + _CACHE_TTL_SECONDS, copy.deepcopy(value))


def _fetch_json(url: str) -> dict[str, Any]:
    _validate_outbound_oidc_url(url)
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
    )
    try:
        with _oidc_opener().open(req, timeout=10) as resp:
            payload = json.loads(
                resp.read().decode("utf-8"),
                parse_constant=_reject_non_finite_json_constant,
            )
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise OIDCAuthError(f"Failed to reach OIDC endpoint: {url}", status_code=502) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise OIDCAuthError(f"OIDC endpoint returned invalid JSON: {url}", status_code=502) from exc
    return payload if isinstance(payload, dict) else {}


def _post_form_json(url: str, form_data: dict[str, Any]) -> dict[str, Any]:
    _validate_outbound_oidc_url(url)
    body = urllib.parse.urlencode(form_data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with _oidc_opener().open(req, timeout=10) as resp:
            payload = json.loads(
                resp.read().decode("utf-8"),
                parse_constant=_reject_non_finite_json_constant,
            )
    except (urllib.error.HTTPError, urllib.error.URLError) as exc:
        raise OIDCAuthError("Failed to exchange the OIDC authorization code", status_code=502) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise OIDCAuthError("OIDC token endpoint returned invalid JSON", status_code=502) from exc
    return payload if isinstance(payload, dict) else {}


def _oidc_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def _is_oidc_issuer_allowed_private_host(hostname: str) -> bool:
    """Check whether *hostname* is the configured issuer's host under
    the ``allow_private_endpoints`` opt-in.

    When ``webui_oidc.allow_private_endpoints`` is enabled, only the
    configured issuer's own hostname is permitted to resolve to private
    / loopback / link-local addresses.  Any other host — including those
    returned by the discovery document (``token_endpoint``, ``jwks_uri``)
    — is still subject to the full SSRF check, preventing discovery-
    controlled pivots to metadata, internal, or loopback endpoints.
    """
    try:
        cfg = _resolve_oidc_config()
        if not cfg.get("allow_private_endpoints"):
            return False
        issuer = str(cfg.get("issuer") or "").strip()
        if not issuer:
            return False
        issuer_hostname = urllib.parse.urlparse(issuer).hostname
        if not issuer_hostname:
            return False
        return hostname == issuer_hostname
    except Exception:
        logger.debug("Failed to read allow_private_endpoints config", exc_info=True)
        return False


def _validate_outbound_oidc_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise OIDCAuthError("OIDC endpoint URLs must use https", status_code=502)
    if parsed.username or parsed.password:
        raise OIDCAuthError("OIDC endpoint URLs must not contain credentials", status_code=502)
    hostname = str(parsed.hostname or "").strip()
    if not hostname:
        raise OIDCAuthError("OIDC endpoint URL was missing a hostname", status_code=502)
    if _is_oidc_issuer_allowed_private_host(hostname):
        return
    if _is_disallowed_oidc_host(hostname):
        raise OIDCAuthError(
            "OIDC endpoint URLs must not target private or local addresses",
            status_code=502,
        )


def _is_disallowed_oidc_host(hostname: str) -> bool:
    literal_ip = _parse_ip_address(hostname)
    if literal_ip is not None:
        return _is_disallowed_oidc_ip(literal_ip)
    try:
        infos = socket.getaddrinfo(hostname, 443, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    for info in infos:
        sockaddr = info[4]
        address = _parse_ip_address(sockaddr[0] if sockaddr else "")
        if address is not None and _is_disallowed_oidc_ip(address):
            return True
    return False


def _parse_ip_address(value: str):
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def _is_disallowed_oidc_ip(address) -> bool:
    candidate = getattr(address, "ipv4_mapped", None) or address
    return (
        candidate.is_loopback
        or candidate.is_private
        or candidate.is_link_local
        or candidate.is_multicast
        or candidate.is_unspecified
        or candidate.is_reserved
    )


def _reject_non_finite_json_constant(value: str):
    raise ValueError(f"OIDC JSON contained unsupported constant: {value}")


def _validate_id_token(
    token: str,
    *,
    client_id: str,
    issuer: str,
    nonce: str,
    jwks_uri: str,
) -> dict[str, Any]:
    header, claims, signed, signature = _parse_jwt(token)
    alg = str(header.get("alg") or "").strip()
    if not alg or alg == "none":
        raise OIDCAuthError("OIDC id_token uses an unsupported signing algorithm")
    jwks = _get_jwks_document(jwks_uri)
    try:
        public_key = _select_public_key(jwks, header)
    except OIDCAuthError as exc:
        if "did not contain the signing key" not in str(exc):
            raise
        jwks = _get_jwks_document(jwks_uri, force_refresh=True)
        public_key = _select_public_key(jwks, header)
    _verify_jwt_signature(public_key, alg, signed, signature)
    _validate_registered_claims(claims, client_id=client_id, issuer=issuer, nonce=nonce)
    if not str(claims.get("sub") or "").strip():
        raise OIDCAuthError("OIDC id_token did not include a subject")
    return claims


def _parse_jwt(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = token.split(".")
    if len(parts) != 3:
        raise OIDCAuthError("OIDC id_token was not a JWT")
    header_b64, payload_b64, signature_b64 = parts
    try:
        header = json.loads(
            _b64u_decode(header_b64),
            parse_constant=_reject_non_finite_json_constant,
        )
        claims = json.loads(
            _b64u_decode(payload_b64),
            parse_constant=_reject_non_finite_json_constant,
        )
        signature = _b64u_decode_bytes(signature_b64)
    except Exception as exc:
        raise OIDCAuthError("OIDC id_token could not be decoded") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise OIDCAuthError("OIDC id_token payload was malformed")
    signed = f"{header_b64}.{payload_b64}".encode("ascii")
    return header, claims, signed, signature


def _select_public_key(jwks: dict[str, Any], header: dict[str, Any]):
    keys = jwks.get("keys")
    if not isinstance(keys, list) or not keys:
        raise OIDCAuthError("OIDC JWKS did not contain any signing keys", status_code=502)
    kid = str(header.get("kid") or "").strip()
    alg = str(header.get("alg") or "").strip()
    matches = []
    for key in keys:
        if not isinstance(key, dict):
            continue
        if key.get("use") not in (None, "sig"):
            continue
        if kid and str(key.get("kid") or "").strip() != kid:
            continue
        if key.get("alg") not in (None, alg):
            continue
        if not _jwk_matches_alg_family(key, alg):
            continue
        matches.append(key)
    if not matches:
        raise OIDCAuthError("OIDC JWKS did not contain the signing key for this id_token", status_code=502)
    return _jwk_to_public_key(matches[0])


def _jwk_matches_alg_family(jwk: dict[str, Any], alg: str) -> bool:
    kty = str(jwk.get("kty") or "").strip()
    if alg.startswith("RS"):
        return kty == "RSA"
    if alg.startswith("ES"):
        return kty == "EC" and str(jwk.get("crv") or "").strip() == _ec_curve_for_alg(alg)
    return True


def _ec_curve_for_alg(alg: str) -> str:
    return {
        "ES256": "P-256",
        "ES384": "P-384",
        "ES512": "P-521",
    }.get(alg, "")


def _jwk_to_public_key(jwk: dict[str, Any]):
    kty = str(jwk.get("kty") or "").strip()
    if kty == "RSA":
        n = _int_from_b64u(jwk.get("n"))
        e = _int_from_b64u(jwk.get("e"))
        return rsa.RSAPublicNumbers(e, n).public_key()
    if kty == "EC":
        crv = str(jwk.get("crv") or "").strip()
        curve = {
            "P-256": ec.SECP256R1(),
            "P-384": ec.SECP384R1(),
            "P-521": ec.SECP521R1(),
        }.get(crv)
        if curve is None:
            raise OIDCAuthError(f"Unsupported OIDC EC curve: {crv}", status_code=502)
        x = _int_from_b64u(jwk.get("x"))
        y = _int_from_b64u(jwk.get("y"))
        return ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
    raise OIDCAuthError(f"Unsupported OIDC key type: {kty}", status_code=502)


def _verify_jwt_signature(public_key, alg: str, signed: bytes, signature: bytes) -> None:
    try:
        if alg == "RS256":
            public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())
            return
        if alg == "RS384":
            public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA384())
            return
        if alg == "RS512":
            public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA512())
            return
        if alg == "ES256":
            public_key.verify(_jose_ecdsa_signature_to_der(signature, 32), signed, ec.ECDSA(hashes.SHA256()))
            return
        if alg == "ES384":
            public_key.verify(_jose_ecdsa_signature_to_der(signature, 48), signed, ec.ECDSA(hashes.SHA384()))
            return
        if alg == "ES512":
            public_key.verify(_jose_ecdsa_signature_to_der(signature, 66), signed, ec.ECDSA(hashes.SHA512()))
            return
    except InvalidSignature as exc:
        raise OIDCAuthError("OIDC id_token signature verification failed") from exc
    raise OIDCAuthError(f"Unsupported OIDC signing algorithm: {alg}", status_code=502)


def _jose_ecdsa_signature_to_der(signature: bytes, part_size: int) -> bytes:
    if len(signature) != part_size * 2:
        raise OIDCAuthError("OIDC id_token ECDSA signature was malformed")
    r = int.from_bytes(signature[:part_size], "big")
    s = int.from_bytes(signature[part_size:], "big")
    return utils.encode_dss_signature(r, s)


def _validate_registered_claims(
    claims: dict[str, Any],
    *,
    client_id: str,
    issuer: str,
    nonce: str,
) -> None:
    now = time.time()
    if str(claims.get("iss") or "").strip() != issuer:
        raise OIDCAuthError("OIDC id_token issuer did not match the configured issuer")
    aud = claims.get("aud")
    if isinstance(aud, list):
        audiences = [str(item) for item in aud]
    elif aud is None:
        audiences = []
    else:
        audiences = [str(aud)]
    if client_id not in audiences:
        raise OIDCAuthError("OIDC id_token audience did not include this client")
    if len(audiences) > 1 and str(claims.get("azp") or "").strip() not in {"", client_id}:
        raise OIDCAuthError("OIDC id_token azp did not match this client")
    exp = _coerce_numeric_claim(claims, "exp")
    if exp is None or exp < now - _CLOCK_SKEW_SECONDS:
        raise OIDCAuthError("OIDC id_token has expired")
    nbf = _coerce_numeric_claim(claims, "nbf")
    if nbf is not None and nbf > now + _CLOCK_SKEW_SECONDS:
        raise OIDCAuthError("OIDC id_token is not valid yet")
    iat = _coerce_numeric_claim(claims, "iat")
    if iat is not None and iat > now + _CLOCK_SKEW_SECONDS:
        raise OIDCAuthError("OIDC id_token has an invalid issued-at time")
    if str(claims.get("nonce") or "").strip() != nonce:
        raise OIDCAuthError("OIDC id_token nonce did not match the login request")


def _coerce_numeric_claim(claims: dict[str, Any], name: str) -> float | None:
    value = claims.get(name)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise OIDCAuthError(f"OIDC id_token claim {name} was not numeric") from exc
    if not math.isfinite(number):
        raise OIDCAuthError(f"OIDC id_token claim {name} was not numeric")
    return number


def _enforce_allowlist(
    claims: dict[str, Any],
    *,
    allow_claim: str,
    allow_values: list[str],
) -> None:
    if not allow_claim:
        return
    claim_value = _get_claim_path(claims, allow_claim)
    if claim_value is None:
        raise OIDCAuthError("OIDC identity is not allowed", status_code=403)
    actual_values = _claim_values(claim_value)
    if allow_values:
        if not any(value in actual_values for value in allow_values):
            raise OIDCAuthError("OIDC identity is not allowed", status_code=403)
        return
    if not actual_values:
        raise OIDCAuthError("OIDC identity is not allowed", status_code=403)


def _get_claim_path(claims: dict[str, Any], dotted_key: str) -> Any:
    current: Any = claims
    for part in dotted_key.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _claim_values(value: Any) -> set[str]:
    if isinstance(value, (list, tuple, set)):
        return {str(item) for item in value if str(item).strip()}
    if isinstance(value, dict):
        return {str(item) for item in value.values() if str(item).strip()}
    text = str(value or "").strip()
    return {text} if text else set()


def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_decode(data: str) -> str:
    return _b64u_decode_bytes(data).decode("utf-8")


def _b64u_decode_bytes(data: str) -> bytes:
    padded = data + "=" * ((4 - len(data) % 4) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def _int_from_b64u(data: Any) -> int:
    if not data:
        raise OIDCAuthError("OIDC JWKS key was missing a required parameter", status_code=502)
    return int.from_bytes(_b64u_decode_bytes(str(data)), "big")
