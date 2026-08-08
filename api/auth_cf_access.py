"""
Cloudflare Access JWT authentication for Hermes WebUI.

When the WebUI is placed behind a Cloudflare Tunnel with Cloudflare Access
(zero-trust) enabled, every request arrives with a ``Cf-Access-Jwt-Assertion``
header containing a signed JWT. This module validates that JWT against
Cloudflare's public keys and extracts the authenticated user's identity.

Environment variables:
    HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN  CF team domain (e.g. https://your-team.cloudflareaccess.com)
    HERMES_WEBUI_CF_ACCESS_AUD          Application Audience (AUD) tag from CF Access policy
    HERMES_WEBUI_CF_ACCESS_EMAILS       Optional comma-separated allowlist of email addresses

Setting *either* TEAM_DOMAIN or AUD signals *intent* to enable CF Access.
The feature is fully *ready* only when team_domain, AUD, and PyJWT are all available.
In the unready state, auth remains enabled (fail closed) but all tokens are rejected.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

_CF_LOCK = threading.RLock()
_CF_KEYS_CACHE: dict[str, dict[str, object]] = {}    # domain -> {"kid": key_obj}
_CF_KEYS_FETCHED_AT: dict[str, float] = {}           # domain -> monotonic timestamp
_CF_KEYS_INFLIGHT: dict[str, threading.Event] = {}   # domain -> single-flight guard
_CF_KEYS_GENERATION: dict[str, int] = {}             # domain -> generation counter
_CF_KEYS_TTL = 3600            # refresh public keys every hour
_CF_KEYS_MAX_STALE = 21600     # discard stale cache after 6 hours
_JWKS_MAX_BYTES = 1 * 1024 * 1024   # max JWKS response size (1 MB)
_JWKS_MAX_KEYS = 20                  # max keys in a JWKS response
_KID_MAX_LEN = 256                   # max kid string length
_PRINCIPAL_MAX_LEN = 256             # max username / principal string length
_TEAM_LABEL_RE = re.compile(r'^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$')


# ── Env accessors ────────────────────────────────────────────────────────────

def _team_domain_raw() -> str:
    return os.getenv("HERMES_WEBUI_CF_ACCESS_TEAM_DOMAIN", "").rstrip("/").strip()


def _audience() -> str:
    return os.getenv("HERMES_WEBUI_CF_ACCESS_AUD", "").strip()


def _allowed_emails() -> set[str] | None:
    raw = os.getenv("HERMES_WEBUI_CF_ACCESS_EMAILS", "").strip()
    if not raw:
        return None
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


# ── Team domain validation ───────────────────────────────────────────────────

def _validate_team_domain(domain: str) -> str | None:
    """Validate and canonicalize the team domain.

    Must be an https:// URL pointing to a ``<label>.cloudflareaccess.com`` host
    where ``<label>`` is exactly one canonical ASCII DNS label: alphanumeric and
    hyphens, 1-63 characters, no leading/trailing hyphens.

    Rejects paths, ports, credentials, query strings, and fragments.
    Returns the canonical URL (no trailing slash) or None if invalid.
    """
    if not domain:
        return None
    try:
        parsed = urllib.parse.urlparse(domain)
    except ValueError:
        logger.error("CF Access: malformed team domain URL")
        return None
    if parsed.scheme != "https":
        logger.error("CF Access: team domain must use https://, got %s", parsed.scheme)
        return None
    if not parsed.hostname:
        logger.error("CF Access: team domain has no hostname")
        return None
    hostname = parsed.hostname
    if not hostname.endswith(".cloudflareaccess.com"):
        logger.error("CF Access: team domain must be a *.cloudflareaccess.com host, got %s", hostname)
        return None

    # Extract the single team label before ".cloudflareaccess.com"
    suffix = ".cloudflareaccess.com"
    team_label = hostname[: -len(suffix)]

    # Reject empty label, multi-label (subdomain), double dots
    if not team_label or "." in team_label:
        logger.error("CF Access: team domain must have exactly one label before cloudflareaccess.com, got %s", hostname)
        return None

    # Validate the label is a canonical ASCII DNS label
    if not _TEAM_LABEL_RE.match(team_label):
        logger.error("CF Access: team domain label %r is not a valid DNS label (alphanumeric + hyphens, 1-63 chars)", team_label)
        return None

    # Reject percent-encoding in the hostname (urlparse may leave it encoded)
    if "%" in hostname:
        logger.error("CF Access: team domain must not contain percent-encoding")
        return None

    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        logger.error("CF Access: team domain must not contain credentials, query, or fragment")
        return None
    if parsed.path not in ("", "/"):
        logger.error("CF Access: team domain must not contain a path, got %s", parsed.path)
        return None
    # Reject any port (numeric or malformed). Accessing parsed.port can raise
    # ValueError on non-numeric ports, so guard with try/except.
    try:
        port = parsed.port
    except ValueError:
        logger.error("CF Access: team domain has malformed port")
        return None
    if port is not None:
        logger.error("CF Access: team domain must not specify a port")
        return None
    return f"https://{hostname}"


def _canonical_team_domain() -> str | None:
    """Return the validated, canonical team domain or None if misconfigured."""
    return _validate_team_domain(_team_domain_raw())


# ── Configured intent vs validator readiness ─────────────────────────────────

def _is_pyjwt_available() -> bool:
    """Check if PyJWT is importable."""
    try:
        import jwt  # noqa: F401
        return True
    except ImportError:
        return False


def is_cf_access_configured() -> bool:
    """True if the operator signalled intent to use CF Access.

    Returns True whenever *any* CF Access env var is set (TEAM_DOMAIN or AUD),
    independently from readiness. This ensures is_auth_enabled() stays True
    (fail closed) even with partial or broken configuration.
    """
    return bool(_team_domain_raw() or _audience())


def is_cf_access_ready() -> bool:
    """True only when CF Access is fully ready to validate tokens."""
    return bool(_canonical_team_domain() and _audience() and _is_pyjwt_available())


# ── JWKS (public key) management ─────────────────────────────────────────────

def _get_public_keys(domain: str, *, force_refresh: bool = False) -> dict[str, object]:
    """Fetch CF Access public keys for *domain* as a kid-to-key mapping.

    Single-flight per domain: concurrent callers block on an Event until the
    first caller completes the fetch, then read the result. Uses generation
    counters so a stale slow fetch cannot overwrite a newer result.

    Cached with bounded staleness:
    - Fresh (< _CF_KEYS_TTL): return cached keys immediately.
    - Stale (< _CF_KEYS_MAX_STALE): attempt refresh; on failure return stale.
    - Expired (> _CF_KEYS_MAX_STALE): discard; on failure return empty (reject all).

    All-or-nothing: if ANY key in the JWKS fails to parse or has a duplicate
    kid, the entire batch is rejected (stale cache retained, nothing published).
    """
    now = time.monotonic()

    # Fast path: check fresh cache under lock
    with _CF_LOCK:
        cached = _CF_KEYS_CACHE.get(domain)
        fetched_at = _CF_KEYS_FETCHED_AT.get(domain, 0.0)
        age = now - fetched_at
        if cached is not None and not force_refresh and age < _CF_KEYS_TTL:
            return cached
        if cached is not None and age >= _CF_KEYS_MAX_STALE:
            _CF_KEYS_CACHE.pop(domain, None)
            _CF_KEYS_FETCHED_AT.pop(domain, None)
            cached = None

        # Single-flight: wait if another thread is fetching for this domain
        inflight = _CF_KEYS_INFLIGHT.get(domain)
        # Always claim a generation for ourselves
        my_generation = _CF_KEYS_GENERATION.get(domain, 0) + 1

        if inflight is not None:
            # Another thread is fetching — wait outside lock
            pass
        else:
            # We are the fetcher — claim the flight
            _CF_KEYS_GENERATION[domain] = my_generation
            _CF_KEYS_INFLIGHT[domain] = threading.Event()
            inflight = None

    # If another thread was fetching, wait and re-check
    if inflight is not None:
        inflight.wait(timeout=15)
        with _CF_LOCK:
            # Recheck age under lock - cache may have expired during wait.
            cached = _CF_KEYS_CACHE.get(domain)
            fetched_at = _CF_KEYS_FETCHED_AT.get(domain, 0.0)
            age = time.monotonic() - fetched_at
            if cached is not None and age < _CF_KEYS_MAX_STALE:
                return cached
            # Cache expired or absent - fail closed.
            if cached is not None and age >= _CF_KEYS_MAX_STALE:
                _CF_KEYS_CACHE.pop(domain, None)
                _CF_KEYS_FETCHED_AT.pop(domain, None)
            return {}

    # We are the fetcher
    try:
        certs_url = f"{domain}/cdn-cgi/access/certs"
        req = urllib.request.Request(certs_url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read(_JWKS_MAX_BYTES + 1)
        if len(raw) > _JWKS_MAX_BYTES:
            logger.warning("CF Access: JWKS response exceeds %d bytes, rejecting", _JWKS_MAX_BYTES)
            _publish_keys(domain, my_generation, None)
            return _safe_cached_keys(domain)
        data = json.loads(raw)
    except Exception as exc:
        logger.warning("CF Access: failed to fetch public keys: %s", exc)
        # Keep stale cache, do not update generation
        _signal_done(domain)
        with _CF_LOCK:
            return _safe_cached_keys(domain)

    if not isinstance(data, dict) or not isinstance(data.get("keys"), list):
        logger.warning("CF Access: malformed JWKS response")
        _signal_done(domain)
        with _CF_LOCK:
            return _safe_cached_keys(domain)

    if len(data["keys"]) > _JWKS_MAX_KEYS:
        logger.warning("CF Access: JWKS contains %d keys (max %d), rejecting", len(data["keys"]), _JWKS_MAX_KEYS)
        _publish_keys(domain, my_generation, None)
        return _safe_cached_keys(domain)

    # All-or-nothing parsing: if ANY key fails to parse, reject the entire batch.
    # Also detect and reject duplicate kid values.
    kid_map: dict[str, object] = {}
    seen_kids: set[str] = set()
    for key_dict in data["keys"]:
        if not isinstance(key_dict, dict):
            logger.warning("CF Access: JWK member is not a dict, rejecting entire batch")
            _signal_done(domain)
            with _CF_LOCK:
                return _safe_cached_keys(domain)
        kid = key_dict.get("kid")
        if not kid or not isinstance(kid, str) or len(kid) > _KID_MAX_LEN:
            logger.warning("CF Access: JWK has invalid kid, rejecting entire batch")
            _signal_done(domain)
            with _CF_LOCK:
                return _safe_cached_keys(domain)
        if kid in seen_kids:
            logger.warning("CF Access: duplicate kid %s in JWKS, rejecting entire batch", kid)
            _signal_done(domain)
            with _CF_LOCK:
                return _safe_cached_keys(domain)
        try:
            import jwt.algorithms
            pub_key = jwt.algorithms.RSAAlgorithm.from_jwk(json.dumps(key_dict))
        except Exception:
            logger.warning("CF Access: unparseable key kid=%s, rejecting entire batch", kid)
            _signal_done(domain)
            with _CF_LOCK:
                return _safe_cached_keys(domain)
        seen_kids.add(kid)
        kid_map[kid] = pub_key

    if not kid_map:
        logger.warning("CF Access: no usable public keys in JWKS response")
        _signal_done(domain)
        with _CF_LOCK:
            return _safe_cached_keys(domain)

    _publish_keys(domain, my_generation, kid_map)
    logger.info("CF Access: loaded %d public key(s)", len(kid_map))
    return kid_map


def _publish_keys(domain: str, generation: int, keys: dict[str, object] | None) -> None:
    """Publish fetched keys if our generation is still current."""
    with _CF_LOCK:
        current_gen = _CF_KEYS_GENERATION.get(domain, 0)
        if generation < current_gen:
            # A newer fetch has already completed; skip stale publication
            return
        if keys is not None:
            _CF_KEYS_CACHE[domain] = keys
            _CF_KEYS_FETCHED_AT[domain] = time.monotonic()
        # If keys is None (reject), leave existing cache or absence as-is
    _signal_done(domain)


def _signal_done(domain: str) -> None:
    """Signal waiting threads and clear inflight marker."""
    with _CF_LOCK:
        event = _CF_KEYS_INFLIGHT.pop(domain, None)
    if event is not None:
        event.set()



def _safe_cached_keys(domain: str) -> dict[str, object]:
    """Return cached keys only if not expired by monotonic deadline.
    Atomically checks age, expires, and returns under _CF_LOCK.
    """
    with _CF_LOCK:
        cached = _CF_KEYS_CACHE.get(domain)
        if cached is None:
            return {}
        fetched_at = _CF_KEYS_FETCHED_AT.get(domain, 0.0)
        age = time.monotonic() - fetched_at
        if age >= _CF_KEYS_MAX_STALE:
            _CF_KEYS_CACHE.pop(domain, None)
            _CF_KEYS_FETCHED_AT.pop(domain, None)
            return {}
        return cached

# ── Token validation ─────────────────────────────────────────────────────────

def validate_cf_access_token(token: str) -> dict | None:
    """Validate a CF Access JWT and return the claims dict, or None on failure."""
    if not token:
        return None
    try:
        import jwt as pyjwt
    except ImportError:
        logger.error("CF Access: PyJWT not installed, cannot validate token")
        return None
    domain = _canonical_team_domain()
    aud = _audience()
    if not domain or not aud:
        logger.warning("CF Access: team domain or AUD not configured")
        return None

    # Extract kid from token header for keyed lookup
    try:
        unverified_header = pyjwt.get_unverified_header(token)
    except Exception:
        logger.warning("CF Access: malformed token header")
        return None
    kid = unverified_header.get("kid")
    if not kid or not isinstance(kid, str) or len(kid) > _KID_MAX_LEN:
        logger.warning("CF Access: token missing or invalid kid header")
        return None

    keys = _get_public_keys(domain)
    key = keys.get(kid) if keys else None

    # If kid not in cache, force a single refresh
    if key is None:
        keys = _get_public_keys(domain, force_refresh=True)
        key = keys.get(kid) if keys else None
    if key is None:
        logger.warning("CF Access: no key found for kid")
        return None

    try:
        claims = pyjwt.decode(
            token,
            key=key,
            audience=aud,
            algorithms=["RS256"],
            options={"verify_aud": True},
        )
    except Exception:
        logger.warning("CF Access: token signature/validation failed")
        return None

    if claims.get("iss") != domain:
        logger.warning("CF Access: token issuer mismatch")
        return None

    allowed = _allowed_emails()
    if allowed is not None:
        email = claims.get("email")
        if not isinstance(email, str):
            logger.warning("CF Access: email claim is not a string, rejecting")
            return None
        if email.lower() not in allowed:
            logger.warning("CF Access: email not in allowlist")
            return None

    logger.debug("CF Access: validated token for user")
    return claims


# ── Identity extraction ──────────────────────────────────────────────────────

def get_cf_access_identity(handler) -> dict | None:
    """Extract and validate CF Access identity from request headers.

    Returns a dict with at least ``username`` if valid, or None.
    Rejects tokens whose claims lack both email and sub — do not manufacture
    a shared anonymous principal.
    """
    if not is_cf_access_configured():
        return None
    token = handler.headers.get("Cf-Access-Jwt-Assertion", "")
    if not token:
        cookie_header = handler.headers.get("Cookie", "")
        if cookie_header:
            import http.cookies
            try:
                cookie = http.cookies.SimpleCookie()
                cookie.load(cookie_header)
                morsel = cookie.get("CF_Authorization")
                if morsel:
                    token = morsel.value
            except Exception:
                pass
    if not token:
        return None
    claims = validate_cf_access_token(token)
    if not claims:
        return None
    # Require a usable identity — reject if neither email nor sub is present.
    # Must be a non-empty bounded string (max 256 chars); reject non-string types.
    raw_principal = claims.get("email") or claims.get("sub")
    if not isinstance(raw_principal, str):
        logger.warning("CF Access: principal (email/sub) is not a string, rejecting")
        return None
    principal = raw_principal.strip()
    if not principal or len(principal) > _PRINCIPAL_MAX_LEN:
        logger.warning("CF Access: principal is empty or exceeds %d chars, rejecting", _PRINCIPAL_MAX_LEN)
        return None
    return {
        "username": principal,
        "auth_type": "cf_access",
        "email": principal,
        "claims": claims,
    }
