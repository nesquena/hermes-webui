"""Fail-soft, operator-configured public provider status adapter."""

from __future__ import annotations

import email.utils
import json
import multiprocessing
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit

STATUS_URL_ENV = "HERMES_WEBUI_PROVIDER_STATUS_URL"
MAX_AGE_SECONDS = 10 * 60
MAX_FUTURE_SECONDS = 2 * 60
MAX_BODY_BYTES = 256 * 1024
FETCH_TIMEOUT_SECONDS = 3
DEFAULT_SUCCESS_TTL = 60
MIN_CACHE_TTL = 15
MAX_CACHE_TTL = 300
MAX_DESCRIPTION_LENGTH = 500
_STATUS_MAP = {
    "operational": "operational",
    "degraded": "degraded",
    "partial_outage": "outage",
    "major_outage": "outage",
    "maintenance": "maintenance",
}
_cache: dict[str, tuple[float, dict[str, dict[str, str]], datetime | None]] = {}
_cache_lock = threading.RLock()
_inflight: dict[str, threading.Event] = {}


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def normalize_status_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parts = urlsplit(value.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    if parts.username is not None or parts.password is not None or parts.fragment:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = parts.hostname.lower()
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port is not None and not ((parts.scheme.lower() == "http" and port == 80) or (parts.scheme.lower() == "https" and port == 443)):
        netloc += f":{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "", parts.query, ""))


_normalize_status_url = normalize_status_url


def _timestamp(value: object, now: datetime) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    parsed = parsed.astimezone(timezone.utc)
    if parsed < now - timedelta(seconds=MAX_AGE_SECONDS) or parsed > now + timedelta(seconds=MAX_FUTURE_SECONDS):
        return None
    return parsed


def _parse_status_payload_with_deadline(
    payload: object, *, now: datetime
) -> tuple[dict[str, dict[str, str]], datetime | None]:
    now = now.astimezone(timezone.utc)
    if not isinstance(payload, dict):
        return {}, None
    meta = payload.get("meta")
    data = payload.get("data")
    if not isinstance(meta, dict) or meta.get("version") != "v1" or not isinstance(data, dict) or not isinstance(data.get("providers"), list):
        return {}, None
    generated_at = _timestamp(meta.get("generatedAt"), now)
    if generated_at is None:
        return {}, None
    deadlines = [generated_at + timedelta(seconds=MAX_AGE_SECONDS)]
    result: dict[str, dict[str, str]] = {}
    for row in data["providers"]:
        if not isinstance(row, dict):
            continue
        slug = row.get("slug")
        current_status = row.get("currentStatus")
        source = row.get("source")
        code = current_status.get("code") if isinstance(current_status, dict) else None
        status = _STATUS_MAP.get(code) if isinstance(code, str) else None
        if not isinstance(slug, str) or not slug or status is None or not isinstance(source, dict):
            continue
        checked = source.get("checkedAt")
        checked_at = _timestamp(checked, now)
        if checked_at is None:
            continue
        deadlines.append(checked_at + timedelta(seconds=MAX_AGE_SECONDS))
        clean: dict[str, str] = {"slug": slug, "status": status, "checkedAt": checked}
        description = current_status.get("summary")
        if isinstance(description, str):
            description = " ".join(description.split())[:MAX_DESCRIPTION_LENGTH]
            if description:
                clean["description"] = description
        source_url = normalize_status_url(source.get("statusPageUrl")) or normalize_status_url(source.get("officialUrl"))
        if source_url:
            clean["url"] = source_url
        result[slug] = clean
    return result, min(deadlines) if result else None


def parse_status_payload(payload: object, *, now: datetime | None = None) -> dict[str, dict[str, str]]:
    rows, _ = _parse_status_payload_with_deadline(payload, now=(now or datetime.now(timezone.utc)).astimezone(timezone.utc))
    return rows


_parse_status_payload = parse_status_payload


def select_provider_status(rows: dict[str, dict[str, str]], provider_slug: str) -> dict[str, str] | None:
    row = rows.get(provider_slug)
    return dict(row) if isinstance(row, dict) and row.get("slug") == provider_slug else None


_select_provider_status = select_provider_status


def _retry_seconds(headers: Any, default: int) -> int:
    value = headers.get("Retry-After") if headers else None
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        try:
            date = email.utils.parsedate_to_datetime(value)
            seconds = max(0, int(date.timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            seconds = default
    return max(MIN_CACHE_TTL, min(MAX_CACHE_TTL, seconds))


def _success_seconds(headers: Any) -> int:
    value = headers.get("Cache-Control", "") if headers else ""
    for directive in value.split(","):
        name, _, raw = directive.strip().partition("=")
        if name.lower() == "max-age":
            try:
                return max(MIN_CACHE_TTL, min(MAX_CACHE_TTL, int(raw)))
            except ValueError:
                break
    return DEFAULT_SUCCESS_TTL


def _response_socket(response: Any) -> Any:
    raw = getattr(getattr(response, "fp", None), "raw", None)
    sock = getattr(raw, "_sock", None)
    return sock if callable(getattr(sock, "settimeout", None)) else None


def _read_body(response: Any, *, deadline: float) -> bytes:
    sock = _response_socket(response)
    if sock is None:
        return response.read(MAX_BODY_BYTES + 1)
    chunks: list[bytes] = []
    size = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("provider status response exceeded wall-clock deadline")
        sock.settimeout(remaining)
        chunk = response.read(min(8192, MAX_BODY_BYTES + 1 - size))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BODY_BYTES:
            return b"".join(chunks)


def _fetch_once(url: str) -> tuple[dict[str, dict[str, str]], int, datetime | None]:
    deadline = time.monotonic() + FETCH_TIMEOUT_SECONDS
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Hermes-WebUI/1"})
    opener = urllib.request.build_opener(_NoRedirectHandler)
    try:
        with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            if getattr(response, "status", 200) != 200:
                return {}, _retry_seconds(response.headers, MIN_CACHE_TTL), None
            body = _read_body(response, deadline=deadline)
            if len(body) > MAX_BODY_BYTES:
                return {}, MIN_CACHE_TTL, None
            parsed = json.loads(body.decode("utf-8"))
            rows, freshness_deadline = _parse_status_payload_with_deadline(parsed, now=_wall_now())
            if not rows:
                return {}, MIN_CACHE_TTL, None
            return rows, _success_seconds(response.headers), freshness_deadline
    except urllib.error.HTTPError as exc:
        return {}, _retry_seconds(exc.headers, MIN_CACHE_TTL), None
    except Exception:
        return {}, MIN_CACHE_TTL, None


def _fetch(url: str) -> tuple[dict[str, dict[str, str]], int, datetime | None]:
    result: list[tuple[dict[str, dict[str, str]], int, datetime | None]] = []

    def run() -> None:
        result.append(_fetch_once(url))

    worker = threading.Thread(target=run, name="provider-status-fetch", daemon=True)
    worker.start()
    worker.join(FETCH_TIMEOUT_SECONDS)
    if worker.is_alive():
        return {}, MIN_CACHE_TTL, None
    return result[0] if result else ({}, MIN_CACHE_TTL, None)


def _fetch_process_main(url: str, result_queue) -> None:
    try:
        result_queue.put(("ok", _fetch_once(url)))
    except BaseException as exc:
        result_queue.put(("error", str(exc)))


def _fetch_killable(url: str) -> tuple[dict[str, dict[str, str]], int, datetime | None]:
    """Run the real network fetch in a process that can be terminated."""
    ctx = multiprocessing.get_context("spawn")
    result_queue = ctx.Queue(maxsize=1)
    process = ctx.Process(target=_fetch_process_main, args=(url, result_queue))
    started = False
    try:
        process.start()
        started = True
        try:
            status, *payload = result_queue.get(timeout=FETCH_TIMEOUT_SECONDS)
        except queue.Empty:
            if process.is_alive():
                process.terminate()
            process.join(timeout=FETCH_TIMEOUT_SECONDS)
            return {}, MIN_CACHE_TTL, None
        process.join(timeout=FETCH_TIMEOUT_SECONDS)
        if process.is_alive():
            process.terminate()
            process.join(timeout=FETCH_TIMEOUT_SECONDS)
        if status != "ok" or not payload:
            return {}, MIN_CACHE_TTL, None
        return payload[0]
    except BaseException:
        if started and process.is_alive():
            process.terminate()
        if started:
            process.join(timeout=FETCH_TIMEOUT_SECONDS)
        return {}, MIN_CACHE_TTL, None
    finally:
        result_queue.close()
        result_queue.join_thread()


_ORIGINAL_FETCH = _fetch


def _wall_now() -> datetime:
    return datetime.now(timezone.utc)


def _fetch_result_parts(result: tuple[Any, ...]) -> tuple[dict[str, dict[str, str]], int, datetime | None]:
    rows, ttl = result[:2]
    freshness_deadline = result[2] if len(result) > 2 else None
    return rows, ttl, freshness_deadline


def _valid_cached_rows(url: str) -> dict[str, dict[str, str]] | None:
    cached = _cache.get(url)
    if cached and cached[0] > time.monotonic() and (cached[2] is None or _wall_now() < cached[2]):
        return cached[1]
    return None


def get_public_provider_statuses(configured_url: object = None, known_slugs: object = None, *, refresh: bool = False) -> dict[str, dict[str, str]]:
    url = normalize_status_url(os.environ.get(STATUS_URL_ENV) if configured_url is None else configured_url)
    if not url:
        return {}
    owner = False
    event = None
    with _cache_lock:
        cached = _cache.get(url)
        if cached and not refresh:
            rows = _valid_cached_rows(url)
            if rows is not None:
                event = None
            else:
                rows = None
        else:
            rows = None
        if rows is None:
            event = _inflight.get(url)
            if event is None:
                event = threading.Event()
                _inflight[url] = event
                owner = True
    if event is not None and not owner:
        event.wait(FETCH_TIMEOUT_SECONDS)
        with _cache_lock:
            rows = _valid_cached_rows(url) or {}
    elif owner:
        try:
            fetcher = _fetch if _fetch is not _ORIGINAL_FETCH else _fetch_killable
            rows, ttl, freshness_deadline = _fetch_result_parts(fetcher(url))
        except Exception:
            rows, ttl, freshness_deadline = {}, MIN_CACHE_TTL, None
        with _cache_lock:
            _cache[url] = (time.monotonic() + ttl, rows, freshness_deadline)
            _inflight.pop(url, None)
            event.set()
    if known_slugs is None:
        return rows
    allowed = set(known_slugs) if not isinstance(known_slugs, str) else {known_slugs}
    return {slug: row for slug, row in rows.items() if slug in allowed}


def clear_status_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _inflight.clear()
