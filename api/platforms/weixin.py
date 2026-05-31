"""Backend module for the Weixin (微信 / 个人微信 / iLink Bot) messaging integration.

Unlike Feishu and WeCom, Weixin credentials are NOT filled in by hand — they are
obtained by scanning a QR code with the WeChat mobile app. This module therefore
drives an in-WebUI **two-step QR login** by reusing the agent's low-level iLink
helpers (never the agent's blocking ``qr_login`` orchestration):

  1. ``start_login()``   — call the agent's ``_api_get`` against ``EP_GET_BOT_QR``
                           to obtain ``qrcode`` (poll token) + ``qrcode_img_content``
                           (the scannable URL); stash them under a generated
                           ``login_id`` in a module-level, thread-safe dict; render
                           the URL to a PNG (base64) via the ``qrcode`` lib when
                           available, else hand back the raw URL.
  2. ``poll_status()``   — call ``_api_get`` against ``EP_GET_QR_STATUS`` with the
                           stored poll token; map iLink's status to a stable state
                           machine (waiting/scanned/expired/confirmed/error); on
                           ``confirmed`` extract {account_id, token, base_url,
                           user_id}, persist via the agent's ``save_weixin_account``
                           AND write WEIXIN_ACCOUNT_ID/WEIXIN_TOKEN/WEIXIN_BASE_URL
                           into the active-profile ``.env``.

Plain form fields (access policies only — never credentials) are persisted the
same way as Feishu/WeCom: read in ``get_config()``, written in ``save()``.

Security rules (mirror Feishu/WeCom, Task spec):
  - WEIXIN_TOKEN is NEVER returned to the caller — only ``token_set: bool``.
  - The .env file stays 0600 (handled by ``_write_env_file``).
  - The QR login uses defensive imports + ``inspect`` so that an older agent
    without the low-level helpers degrades gracefully instead of crashing.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import io
import os
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path

# ── Reuse established WebUI helpers (do NOT reinvent) ───────────────────────
from api.profiles import get_active_hermes_home
from api.onboarding import _load_env_file
from api.providers import _write_env_file


_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off", ""}

# How long a pending login_id stays meaningful before we consider it stale.
_LOGIN_TTL_SECONDS = 600


class WeixinConfigError(ValueError):
    """Raised on invalid Weixin configuration input."""


# ── Defensive agent import for the iLink QR low-level helpers ───────────────
# We reuse the agent's primitives but NEVER its blocking ``qr_login`` driver.
# All of these may be missing on an older agent → start_login degrades.
try:  # pragma: no cover - import wiring exercised indirectly
    from gateway.platforms.weixin import (  # type: ignore
        _api_get as _agent_api_get,
        _make_ssl_connector as _agent_make_ssl_connector,
        save_weixin_account as _agent_save_weixin_account,
        EP_GET_BOT_QR as _AGENT_EP_GET_BOT_QR,
        EP_GET_QR_STATUS as _AGENT_EP_GET_QR_STATUS,
        ILINK_BASE_URL as _AGENT_ILINK_BASE_URL,
        QR_TIMEOUT_MS as _AGENT_QR_TIMEOUT_MS,
    )
except Exception:  # ImportError or anything during import
    _agent_api_get = None
    _agent_make_ssl_connector = None
    _agent_save_weixin_account = None
    _AGENT_EP_GET_BOT_QR = None
    _AGENT_EP_GET_QR_STATUS = None
    _AGENT_ILINK_BASE_URL = None
    _AGENT_QR_TIMEOUT_MS = None


def _agent_qr_available() -> bool:
    """True only when every low-level helper we rely on is importable & callable."""
    global _agent_api_get, _agent_make_ssl_connector, _agent_save_weixin_account
    global _AGENT_EP_GET_BOT_QR, _AGENT_EP_GET_QR_STATUS
    global _AGENT_ILINK_BASE_URL, _AGENT_QR_TIMEOUT_MS
    if _agent_api_get is None:
        try:  # lazy re-attempt (agent may be mounted late, like Feishu's probe)
            from gateway.platforms.weixin import (  # noqa: PLC0415
                _api_get as _g,
                _make_ssl_connector as _mk,
                save_weixin_account as _sv,
                EP_GET_BOT_QR as _eq,
                EP_GET_QR_STATUS as _es,
                ILINK_BASE_URL as _bu,
                QR_TIMEOUT_MS as _to,
            )
            _agent_api_get = _g
            _agent_make_ssl_connector = _mk
            _agent_save_weixin_account = _sv
            _AGENT_EP_GET_BOT_QR = _eq
            _AGENT_EP_GET_QR_STATUS = _es
            _AGENT_ILINK_BASE_URL = _bu
            _AGENT_QR_TIMEOUT_MS = _to
        except Exception:
            return False
    return (
        callable(_agent_api_get)
        and callable(_agent_make_ssl_connector)
        and callable(_agent_save_weixin_account)
        and bool(_AGENT_EP_GET_BOT_QR)
        and bool(_AGENT_EP_GET_QR_STATUS)
        and bool(_AGENT_ILINK_BASE_URL)
    )


# ── Module-level, thread-safe pending-login registry ────────────────────────
# login_id -> {qrcode_value, qrcode_url, base_url, state, created_at, account_id?}
_PENDING: dict[str, dict] = {}
_PENDING_LOCK = threading.Lock()


def _prune_pending(now: float | None = None) -> None:
    """Drop stale pending logins (best-effort; caller may hold the lock)."""
    now = now if now is not None else time.time()
    stale = [
        lid
        for lid, rec in _PENDING.items()
        if now - rec.get("created_at", 0) > _LOGIN_TTL_SECONDS
    ]
    for lid in stale:
        _PENDING.pop(lid, None)


# ── Helpers ────────────────────────────────────────────────────────────────


def _env_path() -> Path:
    return get_active_hermes_home() / ".env"


def _parse_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    return default


def _accounts_dir() -> Path:
    return get_active_hermes_home() / "weixin" / "accounts"


def _detected_account_id() -> str:
    """Return a connected account id by scanning ~/.hermes/weixin/accounts/.

    A persisted ``{account_id}.json`` (excluding the ``.context-tokens.json``
    sidecars) means a successful QR login has happened. Prefer the account id
    recorded in the .env when it is present, else the most recent account file.
    """
    env = _load_env_file(_env_path())
    env_account = (env.get("WEIXIN_ACCOUNT_ID", "") or "").strip()
    accounts_dir = _accounts_dir()
    files: list[Path] = []
    if accounts_dir.is_dir():
        for p in accounts_dir.glob("*.json"):
            if p.name.endswith(".context-tokens.json"):
                continue
            files.append(p)
    if env_account:
        for p in files:
            if p.stem == env_account:
                return env_account
        # env says an account exists even if the file is absent — trust it.
        return env_account
    if not files:
        return ""
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0].stem


def _render_qr_png(data: str) -> str | None:
    """Render ``data`` to a base64 PNG using the ``qrcode`` lib, or None.

    ``qrcode`` is optional in the runtime venv; when unavailable (or rendering
    fails for any reason) we return ``None`` so the caller can fall back to the
    raw scannable URL.
    """
    if not data:
        return None
    try:
        import qrcode  # type: ignore
    except Exception:
        return None
    try:
        img = qrcode.make(data)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        return None


def _run_coro(coro):
    """Run an async coroutine to completion from this synchronous context."""
    return asyncio.run(coro)


async def _fetch_qr() -> dict:
    """Call the agent's ``_api_get`` against EP_GET_BOT_QR. Returns raw dict."""
    import aiohttp  # noqa: PLC0415 (agent dependency; present whenever helpers are)

    connector = _agent_make_ssl_connector() if _agent_make_ssl_connector else None
    timeout_ms = _AGENT_QR_TIMEOUT_MS or 35_000
    async with aiohttp.ClientSession(trust_env=True, connector=connector) as session:
        return await _agent_api_get(
            session,
            base_url=_AGENT_ILINK_BASE_URL,
            endpoint=f"{_AGENT_EP_GET_BOT_QR}?bot_type=3",
            timeout_ms=timeout_ms,
        )


async def _fetch_status(base_url: str, qrcode_value: str) -> dict:
    """Call the agent's ``_api_get`` against EP_GET_QR_STATUS. Returns raw dict."""
    import aiohttp  # noqa: PLC0415

    connector = _agent_make_ssl_connector() if _agent_make_ssl_connector else None
    timeout_ms = _AGENT_QR_TIMEOUT_MS or 35_000
    async with aiohttp.ClientSession(trust_env=True, connector=connector) as session:
        return await _agent_api_get(
            session,
            base_url=base_url,
            endpoint=f"{_AGENT_EP_GET_QR_STATUS}?qrcode={qrcode_value}",
            timeout_ms=timeout_ms,
        )


# ── Public API ──────────────────────────────────────────────────────────────


def get_config() -> dict:
    """Read WEIXIN_* from the active-profile .env + detect a logged-in account.

    ``configured`` is True once a QR login has produced a persisted account (or
    WEIXIN_ACCOUNT_ID + token are present in the .env). WEIXIN_TOKEN is NEVER
    returned — only ``token_set``.
    """
    env = _load_env_file(_env_path())

    account_id = _detected_account_id()
    token_set = bool(env.get("WEIXIN_TOKEN", "").strip())

    return {
        "configured": bool(account_id) or token_set,
        "account_id": account_id,
        "token_set": token_set,
        "base_url": env.get("WEIXIN_BASE_URL", "") or "",
        "dm_policy": env.get("WEIXIN_DM_POLICY", "open") or "open",
        "allowed_users": env.get("WEIXIN_ALLOWED_USERS", "") or "",
        "group_policy": env.get("WEIXIN_GROUP_POLICY", "disabled") or "disabled",
        "group_allowed_users": env.get("WEIXIN_GROUP_ALLOWED_USERS", "") or "",
        "home_channel": env.get("WEIXIN_HOME_CHANNEL", "") or "",
        "login_available": _agent_qr_available(),
    }


def save(payload: dict) -> dict:
    """Persist Weixin access-policy fields only (never credentials).

    Returns ``{saved: True, fields: [<WEIXIN_* keys written>]}``. Raises
    :class:`WeixinConfigError` on invalid input.
    """
    if not isinstance(payload, dict):
        raise WeixinConfigError("payload must be an object")

    updates: dict[str, str] = {}

    dm_policy = str(payload.get("dm_policy", "open") or "open").strip().lower()
    if dm_policy not in {"open", "pairing", "allowlist", "disabled"}:
        raise WeixinConfigError(
            "dm_policy must be 'open', 'pairing', 'allowlist' or 'disabled'"
        )
    updates["WEIXIN_DM_POLICY"] = dm_policy

    group_policy = str(
        payload.get("group_policy", "disabled") or "disabled"
    ).strip().lower()
    if group_policy not in {"disabled", "open", "allowlist"}:
        raise WeixinConfigError(
            "group_policy must be 'disabled', 'open' or 'allowlist'"
        )
    updates["WEIXIN_GROUP_POLICY"] = group_policy

    if "allowed_users" in payload:
        updates["WEIXIN_ALLOWED_USERS"] = str(payload.get("allowed_users") or "").strip()
    if "group_allowed_users" in payload:
        updates["WEIXIN_GROUP_ALLOWED_USERS"] = str(
            payload.get("group_allowed_users") or ""
        ).strip()
    if "home_channel" in payload:
        updates["WEIXIN_HOME_CHANNEL"] = str(payload.get("home_channel") or "").strip()

    # Drop empty values so we never blank out keys via empty strings.
    updates = {k: v for k, v in updates.items() if v != ""}

    _write_env_file(_env_path(), updates)
    return {"saved": True, "fields": sorted(updates.keys())}


def start_login() -> dict:
    """Begin a two-step QR login. Returns a fresh ``login_id`` + QR payload.

    On success returns ``{login_id, qr_png?|qr_url, state: 'waiting'}``. When the
    agent's low-level helpers are unavailable returns ``{error: ...}`` so the
    frontend can advise ``hermes gateway setup`` instead of crashing.
    """
    if not _agent_qr_available():
        return {
            "error": (
                "QR login is unavailable in the current agent version — "
                "run `hermes gateway setup` to connect Weixin."
            )
        }

    try:
        resp = _run_coro(_fetch_qr())
    except Exception as exc:
        return {"error": f"failed to fetch QR code: {exc}"}

    if not isinstance(resp, dict):
        return {"error": "unexpected QR response from iLink"}

    qrcode_value = str(resp.get("qrcode") or "")
    qrcode_url = str(resp.get("qrcode_img_content") or "")
    if not qrcode_value:
        return {"error": "QR response missing qrcode token"}

    # WeChat scans the full liteapp URL; fall back to the raw token if absent.
    qr_scan_data = qrcode_url or qrcode_value

    login_id = secrets.token_urlsafe(16)
    record = {
        "qrcode_value": qrcode_value,
        "qrcode_url": qrcode_url,
        "base_url": _AGENT_ILINK_BASE_URL,
        "state": "waiting",
        "created_at": time.time(),
    }
    with _PENDING_LOCK:
        _prune_pending()
        _PENDING[login_id] = record

    out: dict = {"login_id": login_id, "state": "waiting"}
    png = _render_qr_png(qr_scan_data)
    if png:
        out["qr_png"] = png
    # Always include the raw URL too so the frontend can offer a fallback link.
    if qr_scan_data:
        out["qr_url"] = qr_scan_data
    return out


def poll_status(login_id: str) -> dict:
    """Poll a pending QR login. Maps iLink status → a stable state machine.

    States: ``waiting`` / ``scanned`` / ``expired`` / ``confirmed`` / ``error``.
    On ``confirmed`` the credentials are persisted (agent ``save_weixin_account``
    + WEIXIN_ACCOUNT_ID/WEIXIN_TOKEN/WEIXIN_BASE_URL in .env) and ``account_id``
    is returned. Unknown ``login_id`` → ``{state: 'error', error: ...}``.
    """
    if not login_id or not isinstance(login_id, str):
        return {"state": "error", "error": "login_id is required"}

    with _PENDING_LOCK:
        _prune_pending()
        record = _PENDING.get(login_id)
        if record is None:
            return {"state": "error", "error": "unknown or expired login_id"}
        # A terminal state is sticky — return it without re-polling iLink.
        if record["state"] in {"confirmed", "expired"}:
            result = {"state": record["state"]}
            if record.get("account_id"):
                result["account_id"] = record["account_id"]
            return result
        qrcode_value = record["qrcode_value"]
        base_url = record.get("base_url") or _AGENT_ILINK_BASE_URL

    if not _agent_qr_available():
        return {"state": "error", "error": "QR login is unavailable in the current agent version"}

    try:
        resp = _run_coro(_fetch_status(base_url, qrcode_value))
    except Exception as exc:
        # Transient network error — keep the login alive, report it softly.
        return {"state": "waiting", "detail": f"poll error: {exc}"}

    if not isinstance(resp, dict):
        return {"state": "waiting", "detail": "unexpected status response"}

    status = str(resp.get("status") or "wait")

    if status == "scaned_but_redirect":
        redirect_host = str(resp.get("redirect_host") or "").strip()
        if redirect_host:
            with _PENDING_LOCK:
                rec = _PENDING.get(login_id)
                if rec is not None:
                    rec["base_url"] = f"https://{redirect_host}"
        return {"state": "scanned"}

    if status == "scaned":
        return {"state": "scanned"}

    if status == "wait":
        return {"state": "waiting"}

    if status == "expired":
        with _PENDING_LOCK:
            rec = _PENDING.get(login_id)
            if rec is not None:
                rec["state"] = "expired"
        return {"state": "expired"}

    if status == "confirmed":
        account_id = str(resp.get("ilink_bot_id") or "").strip()
        token = str(resp.get("bot_token") or "").strip()
        cred_base_url = str(resp.get("baseurl") or _AGENT_ILINK_BASE_URL).strip()
        user_id = str(resp.get("ilink_user_id") or "").strip()
        if not account_id or not token:
            return {"state": "error", "error": "QR confirmed but credential payload was incomplete"}

        # Persist via the agent's own account store (writes 0600 JSON).
        try:
            _agent_save_weixin_account(
                str(get_active_hermes_home()),
                account_id=account_id,
                token=token,
                base_url=cred_base_url,
                user_id=user_id,
            )
        except Exception as exc:
            return {"state": "error", "error": f"failed to persist account: {exc}"}

        # Mirror the connection into the active-profile .env (0600 via helper).
        try:
            _write_env_file(
                _env_path(),
                {
                    "WEIXIN_ACCOUNT_ID": account_id,
                    "WEIXIN_TOKEN": token,
                    "WEIXIN_BASE_URL": cred_base_url,
                },
            )
        except Exception as exc:
            return {"state": "error", "error": f"failed to write .env: {exc}"}

        with _PENDING_LOCK:
            rec = _PENDING.get(login_id)
            if rec is not None:
                rec["state"] = "confirmed"
                rec["account_id"] = account_id
        return {"state": "confirmed", "account_id": account_id}

    # Unknown/unexpected status — keep waiting rather than failing hard.
    return {"state": "waiting", "detail": f"status={status}"}


def restart_gateway() -> dict:
    """Run ``hermes gateway restart`` targeting the active profile.

    Locates the CLI via PATH then ``~/.local/bin/hermes``. Never raises for an
    operational failure — always returns ``{ok, detail}``.
    """
    hermes = shutil.which("hermes")
    if not hermes:
        fallback = Path.home() / ".local" / "bin" / "hermes"
        if fallback.exists():
            hermes = str(fallback)
    if not hermes:
        return {"ok": False, "detail": "hermes CLI not found"}

    env = {**os.environ, "HERMES_HOME": str(get_active_hermes_home())}
    try:
        proc = subprocess.run(
            [hermes, "gateway", "restart"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "hermes gateway restart timed out"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}

    detail = (proc.stdout or "").strip() or (proc.stderr or "").strip()
    return {"ok": proc.returncode == 0, "detail": detail}


# ``inspect`` is imported to support defensive signature checks if a future
# agent changes ``_api_get``'s keyword contract; kept referenced to avoid lints.
_ = inspect
