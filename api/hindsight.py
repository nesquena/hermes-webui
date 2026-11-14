"""Server-side Hindsight integration for the WebUI Memory panel.

The browser never receives the Hindsight credential.  This module reads the
active Hermes profile's Hindsight config and profile-scoped secret, validates
the configured upstream, and exposes a small, redacted proxy surface.
"""
from __future__ import annotations

import hashlib
import ipaddress
import http.client
import json
import logging
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_API = "https://hindsight.hadm.net"
DEFAULT_BANK = "shared-agent-memory"
UA = "hindsight-client/0.6.1"
UPSTREAM_TIMEOUT = 90.0
STATUS_TTL = 30.0
MAX_QUERY_CHARS = 4000
MAX_CONTEXT_CHARS = 4000
MAX_RETAIN_CHARS = 20000
MAX_TAGS = 20
MAX_TAG_CHARS = 64
MAX_DOCUMENT_ID_CHARS = 128
_BANK_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DOCUMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_TAG_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")

# Provider calls are deliberately bounded separately from WebUI request workers.
# A slow Hindsight backend may consume at most four outbound slots; callers get
# a bounded response instead of creating an unbounded pile of socket threads.
_UPSTREAM_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="hindsight-proxy")
_HINDSIGHT_MAX_PENDING = 16
_HINDSIGHT_SEM = __import__('threading').Semaphore(_HINDSIGHT_MAX_PENDING)
_RATE_LOCK = threading.Lock()
_RATE_STATE: dict[tuple[str, str], list[float]] = {}
_RATE_STATE_MAX = 1024
_STATUS_LOCK = threading.Lock()
_STATUS_CACHE: dict[tuple[str, str, str, str, str, bool], tuple[float, dict[str, Any]]] = {}
_STATUS_CACHE_MAX = 256


def _resolve_public_addresses(host: str, port: int | None) -> tuple[str, ...]:
    """Resolve an origin immediately before connecting and reject private IPs."""
    resolved = tuple({item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})
    if not resolved or any(not _is_public_address(address) for address in resolved):
        raise ValueError("Hindsight API host resolves to a private or local address")
    return resolved


def _active_home() -> Path:
    try:
        from api.profiles import get_active_hermes_home
        return Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        return (Path.home() / ".hermes").resolve()


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _profile_secret(name: str, home: Path) -> str:
    """Read only the active profile's secret, never another profile's env.

    ``get_secret`` falls back to raw ``os.environ`` when no profile scope is
    installed (see agent.secret_scope docstring) — that's the process-wide
    env, which for a named (non-default) profile can be the DEFAULT profile's
    key. Enter the same read-only per-request profile scope api/profiles.py
    uses for provider/model reads so this fallback stays profile-isolated.
    """
    env_value = _load_env_file(home / ".env").get(name, "")
    if env_value:
        return env_value
    try:
        from agent.secret_scope import get_secret
        from api.profiles import profile_env_for_active_request_readonly
        with profile_env_for_active_request_readonly("hindsight secret read"):
            return str(get_secret(name, "") or "")
    except Exception:
        return ""


def _validate_bank_id(value: Any) -> str:
    bank = str(value or "").strip()
    if not _BANK_RE.fullmatch(bank):
        raise ValueError("Invalid Hindsight bank ID")
    return bank


def _is_public_address(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
        or str(address) == "169.254.169.254"
    )


def _validate_api_url(value: Any) -> str:
    url = str(value or "").strip().rstrip("/")
    if not url:
        url = DEFAULT_API
    parts = urllib.parse.urlsplit(url)
    allow_insecure = os.environ.get("HERMES_WEBUI_HINDSIGHT_ALLOW_INSECURE") == "1"
    allowed_schemes = {"https"} | ({"http"} if allow_insecure else set())
    if parts.scheme not in allowed_schemes or not parts.hostname or parts.username or parts.password:
        raise ValueError("Hindsight API URL must be an HTTPS origin")
    if parts.query or parts.fragment or parts.path not in ("", "/"):
        raise ValueError("Hindsight API URL must be an origin without a path")
    host = parts.hostname.rstrip(".").lower()
    if not _is_public_address(host):
        raise ValueError("Hindsight API URL must not target a private or local address")
    # Resolve hostnames too: a DNS name that resolves to localhost/private space
    # is no safer than a literal private address.
    try:
        _resolve_public_addresses(host, parts.port)
    except OSError as exc:
        raise ValueError("Hindsight API host could not be resolved") from exc
    return url


_CONFIG_UNREADABLE = object()  # sentinel: file exists but failed to parse


def _parse_config(home: Path) -> dict[str, Any]:
    path = home / "hindsight" / "config.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Unable to read Hindsight config for profile %s: %s", home.name, type(exc).__name__)
        return _CONFIG_UNREADABLE
    return data if isinstance(data, dict) else {}


def _hindsight_config() -> dict[str, Any]:
    """Resolve and validate the active profile's non-secret Hindsight config.

    Resolves provider/memory_enabled/api_key (all local, no network) FIRST,
    and only calls _validate_api_url/_validate_bank_id — which resolve DNS
    via socket.getaddrinfo — when the profile is actually plausibly enabled.
    Every GET /api/memory load calls this (see api/routes.py) even for
    profiles that never configure Hindsight; unconditionally resolving DNS
    here blocked the whole (otherwise-local) Memory panel on an unrelated
    optional service, including on offline installs.
    """
    home = _active_home()
    data = _parse_config(home)
    # A truncated/corrupted config.json must fail closed, not silently fall
    # through to DEFAULT_API/DEFAULT_BANK below with a still-valid api_key —
    # that would send a profile's private memory to the wrong (public
    # default) service instead of its configured self-hosted upstream.
    config_unreadable = data is _CONFIG_UNREADABLE
    if config_unreadable:
        data = {}
    env = _load_env_file(home / ".env")
    api_key = _profile_secret("HINDSIGHT_API_KEY", home)

    provider = ""
    memory_enabled = True
    try:
        from api.config import get_config_snapshot
        cfg = get_config_snapshot()
        memory = cfg.get("memory") if isinstance(cfg, dict) else {}
        if isinstance(memory, dict):
            provider = str(memory.get("provider") or "")
            _memory_enabled_raw = memory.get("memory_enabled", True)
            if isinstance(_memory_enabled_raw, str):
                # An env-expanded value like "${MEMORY_ENABLED}" can resolve to
                # the STRING "false" — bool("false") is True, which would keep
                # Hindsight active despite an explicit opt-out. Reuse the same
                # normalization api/routes.py's Memory routes apply.
                from api.routes import _webui_truthy
                memory_enabled = _webui_truthy(_memory_enabled_raw)
            else:
                memory_enabled = bool(_memory_enabled_raw)
    except Exception as exc:
        logger.debug("Hindsight provider config lookup failed: %s", type(exc).__name__)

    would_be_enabled = (not config_unreadable) and provider == "hindsight" and memory_enabled and bool(api_key)

    raw_api_url = data.get("api_url") or data.get("apiUrl") or env.get("HINDSIGHT_API_URL") or DEFAULT_API
    bank_value = data.get("bank_id") or data.get("bankId") or data.get("bank")
    if isinstance(bank_value, dict):
        bank_value = bank_value.get("bankId")
    if not bank_value:
        banks = data.get("banks")
        if isinstance(banks, dict) and isinstance(banks.get("hermes"), dict):
            bank_value = banks["hermes"].get("bankId")
    raw_bank_id = bank_value or env.get("HINDSIGHT_BANK_ID") or DEFAULT_BANK

    if would_be_enabled:
        api_url = _validate_api_url(raw_api_url)
        bank_id = _validate_bank_id(raw_bank_id)
    else:
        # Not enabled regardless of what the upstream host validates to —
        # skip DNS/private-address resolution and any bank-id shape check;
        # these values are display-only in this state (the disabled hint UI).
        api_url = str(raw_api_url or DEFAULT_API).strip().rstrip("/")
        bank_id = str(raw_bank_id or DEFAULT_BANK).strip()

    return {
        "api_url": api_url,
        "bank_id": bank_id,
        "api_key_present": bool(api_key),
        "_api_key": api_key,
        "mode": data.get("mode", "local_external"),
        "memory_mode": data.get("memory_mode", "hybrid"),
        "recall_budget": data.get("recall_budget", "mid"),
        "provider": provider,
        "memory_enabled": memory_enabled,
        "enabled": would_be_enabled,
    }


def _bank_path(cfg: dict[str, Any], suffix: str) -> str:
    bank = urllib.parse.quote(cfg["bank_id"], safe="")
    return f"/v1/default/banks/{bank}/{suffix.lstrip('/')}"


def _redact_detail(detail: Any) -> str:
    text = str(detail or "upstream request failed")
    try:
        from api.helpers import _redact_text
        text = _redact_text(text)
    except Exception:
        pass
    return text[:500]


def _coerce_upstream_text(value: Any) -> str:
    """Coerce an upstream text field to a plain string.

    Upstream is a trust boundary: a malformed or compromised response can
    return a truthy non-string 'text' (e.g. a dict). The browser calls
    .slice()/.replace() on recall/reflect text unconditionally and throws,
    breaking the whole Memory panel — coerce here instead of passing it
    through.
    """
    return value if isinstance(value, str) else ""


def _coerce_upstream_tags(value: Any) -> list[str]:
    """Coerce an upstream tags field to a list of plain strings.

    Same trust-boundary reasoning as _coerce_upstream_text, for the one
    remaining field the panel treats as a fixed shape. The recent-memories
    renderer guards only on truthiness and .length, so a truthy non-list --
    a bare string is the easy case -- clears that guard, survives .slice(),
    then throws on .join() and takes the whole Memory panel down. The recall
    renderer happens to use Array.isArray, but the boundary should not depend
    on which renderer is luckier.
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _coerce_upstream_count(value: Any) -> int | None:
    """Coerce an upstream count field to a plain non-negative int, else None.

    Companion to _coerce_upstream_text for numeric fields: bool is excluded
    (it is an int subclass and would render as True/False), and anything that
    is not a clean non-negative integer is dropped rather than forwarded.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _redact_success_value(value: Any) -> Any:
    """Redact strings recursively before returning upstream memory to the browser."""
    try:
        from api.helpers import _redact_text
    except Exception:
        return value
    if isinstance(value, str):
        return _redact_text(value)
    if isinstance(value, list):
        return [_redact_success_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_success_value(item) for key, item in value.items()}
    return value


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so an upstream cannot bounce the bearer to another host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _PinnedConnectionMixin:
    """Shared connect path for HTTP(S), using only freshly validated addresses."""

    def _connect_pinned(self):
        last_error = None
        for address in _resolve_public_addresses(self.host, self.port):
            try:
                self.sock = socket.create_connection((address, self.port), self.timeout, self.source_address)
                break
            except OSError as exc:
                last_error = exc
        else:
            if last_error is not None:
                raise last_error
            raise OSError("could not connect to a validated Hindsight host")


class _PinnedHTTPConnection(_PinnedConnectionMixin, http.client.HTTPConnection):
    """Pin HTTP too when insecure development mode is explicitly enabled."""

    def connect(self):
        self._connect_pinned()


class _PinnedHTTPSConnection(_PinnedConnectionMixin, http.client.HTTPSConnection):
    """Dial a freshly validated IP while preserving hostname verification and SNI."""

    def connect(self):
        self._connect_pinned()
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return self.do_open(_PinnedHTTPConnection, req)


def _open_pinned(request: urllib.request.Request, *, timeout: float):
    """Open a request without proxies, redirects, or post-check DNS resolution."""
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirectHandler(), _PinnedHTTPHandler(), _PinnedHTTPSHandler()
    )
    return opener.open(request, timeout=timeout)


def _proxy_sync(method: str, path: str, api_key: str, api_url: str, body: dict[str, Any] | None = None, timeout: float = UPSTREAM_TIMEOUT) -> tuple[int, dict[str, Any]]:
    url = api_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json", "User-Agent": UA}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _open_pinned(request, timeout=timeout) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                return 502, {"detail": "Hindsight response too large"}
            try:
                parsed = json.loads(raw.decode("utf-8", errors="replace"))
            except (TypeError, ValueError):
                return 502, {"detail": "Hindsight returned a non-JSON response"}
            return int(response.status), parsed if isinstance(parsed, dict) else {"data": parsed}
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096) if hasattr(exc, "read") else b""
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        except (TypeError, ValueError):
            parsed = {}
        detail = parsed.get("detail") if isinstance(parsed, dict) else ""
        return int(exc.code), {"detail": _redact_detail(detail or exc.reason)}
    except Exception as exc:
        logger.warning("Hindsight upstream %s failed: %s", method, type(exc).__name__)
        return 599, {"detail": "Hindsight upstream is unavailable"}


def _proxy(*args: Any, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    """Run one upstream call through a bounded executor.

    A semaphore gates submissions so the executor's unbounded queue cannot
    grow without bound. When the semaphore is exhausted we return 503
    immediately (backpressure). Timed-out futures are cancelled when still
    queued; running tasks cannot be cancelled and will release the slot via
    the done-callback when they finish.
    """
    timeout = float(kwargs.get("timeout", UPSTREAM_TIMEOUT))
    if not _HINDSIGHT_SEM.acquire(blocking=False):
        return 503, {"detail": "Hindsight proxy is busy"}
    try:
        future = _UPSTREAM_EXECUTOR.submit(_proxy_sync, *args, **kwargs)
        # Release semaphore when the work finishes (success, error, or cancel).
        def _release(_f):
            try:
                _HINDSIGHT_SEM.release()
            except ValueError:
                pass
        try:
            future.add_done_callback(_release)
        except Exception:
            pass
        try:
            return future.result(timeout=timeout + 1.0)
        except FutureTimeout:
            # If still queued, cancel to free worker; running task will
            # release via callback when done.
            try:
                future.cancel()
            except Exception:
                pass
            return 504, {"detail": "Hindsight upstream timed out"}
        except RuntimeError:
            return 503, {"detail": "Hindsight proxy is busy"}
    except RuntimeError:
        # Submit itself raised (e.g. executor shutdown) — release semaphore.
        try:
            _HINDSIGHT_SEM.release()
        except ValueError:
            pass
        return 503, {"detail": "Hindsight proxy is busy"}


def _client_key(handler: Any) -> str:
    """Return a rate-limit key aware of reverse proxies.

    When behind a proxy (documented Hermes remote-access mode), client_address
    is the proxy itself. Prefer X-Forwarded-For's leftmost entry when present,
    falling back to the socket peer. This is not a security boundary — it is
    only for rate-limit bucketing.
    """
    try:
        headers = getattr(handler, "headers", None)
        if headers is not None:
            # headers may be http.client.HTTPMessage or dict-like
            xff = None
            try:
                xff = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
            except Exception:
                xff = None
            if xff:
                # Leftmost is original client
                candidate = xff.split(",")[0].strip()
                if candidate:
                    return candidate
    except Exception:
        pass
    return str(getattr(handler, "client_address", ("unknown",))[0])

def _rate_limit(handler: Any, operation: str, limit: int) -> bool:
    now = time.monotonic()
    address = _client_key(handler)
    key = (address, operation)
    with _RATE_LOCK:
        # Time-based sweep: drop expired samples per-key
        samples = [stamp for stamp in _RATE_STATE.get(key, []) if now - stamp < 60.0]
        if len(samples) >= limit:
            _RATE_STATE[key] = samples
            return False
        samples.append(now)
        _RATE_STATE[key] = samples
        # Eviction: cap number of distinct client buckets to prevent unbounded growth
        if len(_RATE_STATE) > _RATE_STATE_MAX:
            # Evict oldest entries: remove keys with oldest last-sample or fewest samples
            # Simplest: evict arbitrary excess keys (first inserted). Ordered dict preserves insertion order.
            excess = len(_RATE_STATE) - _RATE_STATE_MAX
            for k in list(_RATE_STATE.keys())[:excess]:
                if k != key:
                    _RATE_STATE.pop(k, None)
                elif excess > 1:
                    # Don't evict the just-updated key unless we must
                    pass
            # If still over (because we skipped current key), evict it last
            while len(_RATE_STATE) > _RATE_STATE_MAX:
                oldest = next(iter(_RATE_STATE))
                _RATE_STATE.pop(oldest, None)
    return True


def _rate_limited(handler: Any):
    from api.helpers import j
    return j(handler, {"error": "Hindsight request rate limit exceeded", "code": "rate_limited"}, status=429, extra_headers={"Retry-After": "60"})


def _enabled_or_error(handler: Any, cfg: dict[str, Any]):
    from api.helpers import bad
    if cfg["enabled"]:
        return None
    return bad(handler, "Hindsight is not enabled for this profile", 503)


def _validate_query(value: Any) -> str:
    query = str(value or "").strip()
    if not query:
        raise ValueError("query is required")
    if len(query) > MAX_QUERY_CHARS:
        raise ValueError(f"query is too long (max {MAX_QUERY_CHARS} characters)")
    return query


def _validate_tags(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    values = [part.strip() for part in value.split(",") if part.strip()] if isinstance(value, str) else value
    if not isinstance(values, list) or len(values) > MAX_TAGS:
        raise ValueError("tags must be a list of at most 20 values")
    if any(not isinstance(tag, str) or not _TAG_RE.fullmatch(tag) for tag in values):
        raise ValueError("tags contain an invalid value")
    return values


def _error_response(handler: Any, status: int, detail: Any, elapsed_ms: int | None = None):
    from api.helpers import j
    client_status = status if 400 <= status < 500 and status not in (408, 429) else 502
    payload: dict[str, Any] = {"error": _redact_detail(detail), "code": "hindsight_upstream_error"}
    if status == 429:
        client_status = 429
        payload["code"] = "rate_limited"
    if elapsed_ms is not None:
        payload["elapsed_ms"] = elapsed_ms
    headers = {"Retry-After": "60"} if client_status == 429 else None
    return j(handler, payload, status=client_status, extra_headers=headers)


def handle_status(handler: Any):
    from api.helpers import j
    if not _rate_limit(handler, "status", 30):
        return _rate_limited(handler)
    try:
        cfg = _hindsight_config()
    except ValueError as exc:
        return j(handler, {"enabled": False, "reachable": False, "error": _redact_detail(exc)}, status=503)
    payload = {key: cfg[key] for key in ("enabled", "provider", "api_url", "bank_id", "mode", "memory_mode", "recall_budget", "api_key_present")}
    # Key on the full credential identity (a digest, never the raw key) so a
    # provider/key rotation invalidates the cache immediately instead of
    # serving a stale reachable/auth-failure result for up to STATUS_TTL —
    # api_url+bank_id alone can stay identical across a key change.
    key_fingerprint = hashlib.sha256(cfg["_api_key"].encode("utf-8")).hexdigest() if cfg["_api_key"] else ""
    #
    # `enabled` is keyed directly rather than by enumerating its inputs. It
    # selects which branch below produces `result` (the constant disabled
    # hint vs a live upstream probe), and it derives from four things:
    # config readability, provider, memory_enabled, and key presence. The
    # rest of this tuple covers only two of those. Enumerating the inputs
    # has already missed one twice — credential identity, then
    # memory_enabled — and config-unreadable would be the third (it falls
    # back to the env/default api_url, so the key can stay byte-identical
    # while enabled flips). Keying the derived value that actually gates
    # the branch is correct by construction.
    cache_key = (_active_home().as_posix(), cfg["api_url"], cfg["bank_id"], cfg["provider"], key_fingerprint, bool(cfg["enabled"]))
    now = time.monotonic()
    with _STATUS_LOCK:
        cached = _STATUS_CACHE.get(cache_key)
        if cached and now - cached[0] < STATUS_TTL:
            return j(handler, {**payload, **cached[1]})
    if not cfg["enabled"]:
        result = {"reachable": False, "hint": "Set memory.provider to hindsight and configure HINDSIGHT_API_KEY"}
    else:
        status, data = _proxy("GET", _bank_path(cfg, "memories/list?limit=1"), cfg["_api_key"], cfg["api_url"], timeout=8)
        result = {"reachable": status == 200, "probe_status": status}
        if status == 401 or status == 403:
            result["hint"] = "Hindsight authentication failed"
        elif status >= 500:
            result["hint"] = "Hindsight upstream is unavailable"
        if isinstance(data, dict) and "total" in data:
            # Upstream is a trust boundary. Every other handler routes upstream
            # values through _redact_success_value/_coerce_upstream_text before
            # they reach the browser; this one field was passed through raw, so
            # a malformed or compromised upstream could hand the panel an
            # arbitrary-shaped value for a field the UI renders as a count.
            total = _coerce_upstream_count(data["total"])
            if total is not None:
                result["total_memories"] = total
    with _STATUS_LOCK:
        _STATUS_CACHE[cache_key] = (now, result)
        # Evict oldest entries if unbounded growth (per-profile cache)
        if len(_STATUS_CACHE) > _STATUS_CACHE_MAX:
            excess = len(_STATUS_CACHE) - _STATUS_CACHE_MAX
            for k in list(_STATUS_CACHE.keys())[:excess]:
                _STATUS_CACHE.pop(k, None)
    return j(handler, {**payload, **result})


def handle_recall(handler: Any, body: dict[str, Any]):
    from api.helpers import bad, j
    if not _rate_limit(handler, "recall", 10):
        return _rate_limited(handler)
    try:
        cfg = _hindsight_config()
        query = _validate_query(body.get("query") or body.get("q"))
        budget = body.get("budget") or cfg["recall_budget"] or "mid"
        budget = budget if budget in ("low", "mid", "high") else "mid"
        raw_max = body.get("max_tokens", 12000)
        max_tokens = int(raw_max)
        max_tokens = max(256, min(32000, max_tokens))
        tags = _validate_tags(body.get("tags"))
    except (ValueError, TypeError) as exc:
        return bad(handler, str(exc), 400)
    disabled = _enabled_or_error(handler, cfg)
    if disabled:
        return disabled
    request_body: dict[str, Any] = {"query": query, "budget": budget, "max_tokens": max_tokens}
    if tags:
        request_body["tags"] = tags
    if body.get("trace") is True:
        request_body["trace"] = True
    started = time.monotonic()
    status, data = _proxy("POST", _bank_path(cfg, "memories/recall"), cfg["_api_key"], cfg["api_url"], body=request_body)
    elapsed = round((time.monotonic() - started) * 1000)
    if status != 200:
        return _error_response(handler, status, data.get("detail"), elapsed)
    results = data.get("results") if isinstance(data.get("results"), list) else []
    trimmed = []
    for item in results[:30]:
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key) for key in ("id", "text", "type", "context", "occurred_start", "occurred_end", "mentioned_at", "created_at", "score", "tags", "chunk_id", "document_id")}
        row["text"] = _coerce_upstream_text(row["text"])
        row["tags"] = _coerce_upstream_tags(row.get("tags"))
        trimmed.append(_redact_success_value(row))
    return j(handler, _redact_success_value({"query": query, "budget": budget, "elapsed_ms": elapsed, "count": len(results), "results": trimmed, "trace": data.get("trace"), "entities": data.get("entities")}))


def handle_reflect(handler: Any, body: dict[str, Any]):
    from api.helpers import bad, j
    if not _rate_limit(handler, "reflect", 10):
        return _rate_limited(handler)
    try:
        cfg = _hindsight_config()
        query = _validate_query(body.get("query") or body.get("q"))
        context = str(body.get("context") or "").strip()
        if len(context) > MAX_CONTEXT_CHARS:
            raise ValueError(f"context is too long (max {MAX_CONTEXT_CHARS} characters)")
        budget = body.get("budget") or "low"
        budget = budget if budget in ("low", "mid", "high") else "low"
    except (ValueError, TypeError) as exc:
        return bad(handler, str(exc), 400)
    disabled = _enabled_or_error(handler, cfg)
    if disabled:
        return disabled
    request_body: dict[str, Any] = {"query": query, "budget": budget}
    if context:
        request_body["context"] = context
    if body.get("include_facts") is True:
        request_body["include_facts"] = True
    started = time.monotonic()
    status, data = _proxy("POST", _bank_path(cfg, "reflect"), cfg["_api_key"], cfg["api_url"], body=request_body)
    elapsed = round((time.monotonic() - started) * 1000)
    if status != 200:
        return _error_response(handler, status, data.get("detail"), elapsed)
    reflect_text = _coerce_upstream_text(data.get("text")) or _coerce_upstream_text(data.get("answer"))
    return j(handler, _redact_success_value({"query": query, "budget": budget, "elapsed_ms": elapsed, "text": reflect_text, "based_on": data.get("based_on"), "facts": data.get("facts")}))


def handle_list_memories(handler: Any, parsed: Any):
    from api.helpers import bad, j
    if not _rate_limit(handler, "list", 30):
        return _rate_limited(handler)
    try:
        cfg = _hindsight_config()
    except ValueError as exc:
        return bad(handler, str(exc), 503)
    disabled = _enabled_or_error(handler, cfg)
    if disabled:
        return disabled
    qs = urllib.parse.parse_qs(parsed.query or "")
    try:
        limit = max(1, min(100, int(qs.get("limit", [20])[0])))
        offset = max(0, min(200000, int(qs.get("offset", [0])[0])))
        query = qs.get("q", [""])[0].strip()
        if len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"q is too long (max {MAX_QUERY_CHARS} characters)")
    except (ValueError, TypeError) as exc:
        return bad(handler, str(exc), 400)
    params = urllib.parse.urlencode({"limit": limit, "offset": offset, **({"q": query} if query else {})})
    status, data = _proxy("GET", _bank_path(cfg, f"memories/list?{params}"), cfg["_api_key"], cfg["api_url"], timeout=20)
    if status != 200:
        return _error_response(handler, status, data.get("detail"))
    memories = data.get("memories") or data.get("items") or data.get("results") or []
    memories = memories if isinstance(memories, list) else []
    trimmed = []
    for item in memories[:limit]:
        if isinstance(item, dict):
            trimmed.append(_redact_success_value({"id": item.get("id", ""), "text": str(item.get("text", ""))[:2000], "type": item.get("type", ""), "context": item.get("context", ""), "created_at": item.get("created_at"), "updated_at": item.get("updated_at"), "tags": _coerce_upstream_tags(item.get("tags"))}))
    return j(handler, _redact_success_value({"total": data.get("total", len(trimmed)), "limit": limit, "offset": offset, "memories": trimmed, "query": query}))


def handle_retain(handler: Any, body: dict[str, Any]):
    from api.helpers import bad, j
    if not _rate_limit(handler, "retain", 20):
        return _rate_limited(handler)
    try:
        cfg = _hindsight_config()
        content = str(body.get("content") or "").strip()
        if not content:
            raise ValueError("content is required")
        if len(content) > MAX_RETAIN_CHARS:
            raise ValueError(f"content is too long (max {MAX_RETAIN_CHARS} characters)")
        context = str(body.get("context") or "").strip()
        if len(context) > MAX_CONTEXT_CHARS:
            raise ValueError(f"context is too long (max {MAX_CONTEXT_CHARS} characters)")
        tags = _validate_tags(body.get("tags"))
        document_id = str(body.get("document_id") or "").strip()
        if document_id and not _DOCUMENT_RE.fullmatch(document_id):
            raise ValueError("document_id contains invalid characters")
    except (ValueError, TypeError) as exc:
        return bad(handler, str(exc), 400)
    disabled = _enabled_or_error(handler, cfg)
    if disabled:
        return disabled
    item: dict[str, Any] = {"content": content}
    if context:
        item["context"] = context
    if tags:
        item["tags"] = tags
    request_body: dict[str, Any] = {"items": [item]}
    if document_id:
        request_body["document_id"] = document_id
    logger.info("Hindsight retain requested for bank %s (%d chars)", cfg["bank_id"], len(content))
    status, data = _proxy("POST", _bank_path(cfg, "memories"), cfg["_api_key"], cfg["api_url"], body=request_body, timeout=30)
    if status not in (200, 201, 202):
        return _error_response(handler, status, data.get("detail"))
    return j(handler, {"ok": True})
