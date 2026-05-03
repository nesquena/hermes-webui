"""In-app OAuth flow implementations for providers like OpenAI Codex.

Uses only stdlib (urllib.request, json, time) — no external dependencies.
Credentials are stored in ~/.hermes/auth.json under the credential_pool.
"""

import json
import logging
import time
import uuid
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

logger = logging.getLogger(__name__)

AUTH_JSON_PATH = Path.home() / ".hermes" / "auth.json"

# ── Codex OAuth constants (from hermes_cli/auth.py) ──
CODEX_CLIENT_ID = "pdlLIX2Y72MIl2rhLhTE9VV9bN905kBh"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/device/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_SCOPE = "openid profile email offline_access"
CODEX_GRANT_TYPE_DEVICE = "urn:ietf:params:oauth:grant-type:device_code"


# ── auth.json helpers ──

def _read_auth_json():
    """Read auth.json and return parsed dict, or empty dict."""
    if AUTH_JSON_PATH.exists():
        try:
            return json.loads(AUTH_JSON_PATH.read_text())
        except json.JSONDecodeError as exc:
            logger.warning("Failed to parse %s: %s", AUTH_JSON_PATH, exc)
            return {}
    return {}


def read_auth_json():
    """Public wrapper for _read_auth_json.

    Used by the streaming layer's credential self-heal logic (#1401) to
    re-read credentials without coupling to the private helper.
    """
    return _read_auth_json()


def _write_auth_json(data):
    """Atomically write auth.json via temp-file rename.

    SECURITY: auth.json contains OAuth access/refresh tokens. ``tmp.replace()``
    preserves the temp file's mode (created with the process umask, typically
    0644 or 0664), NOT the prior auth.json mode. Without an explicit chmod,
    tokens land world-readable on shared systems. Set 0600 BEFORE the rename
    so there is no window where the final file is world-readable.
    (Opus pre-release advisor finding.)
    """
    import os, stat
    AUTH_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = AUTH_JSON_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    try:
        tmp.chmod(0o600)
    except OSError as e:
        # Best-effort: if chmod fails (e.g. on a filesystem that doesn't
        # support POSIX modes), don't abort. The startup permission fixer
        # in api.startup will sweep auth.json on the next process start.
        logger.warning("Failed to chmod 0600 on %s: %s", tmp, e)
    tmp.replace(AUTH_JSON_PATH)


# ── Codex device-code flow ──

def start_codex_device_code():
    """Start Codex OAuth device-code flow.

    Returns dict: { device_code, user_code, verification_uri, expires_in, interval }
    Raises RuntimeError on network error.
    """
    params = {
        "client_id": CODEX_CLIENT_ID,
        "scope": CODEX_SCOPE,
    }
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(CODEX_AUTH_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        raise RuntimeError(f"Failed to start Codex OAuth: {e}") from e


def poll_codex_token(device_code, interval=5):
    """Poll for Codex OAuth token. Generator that yields status dicts.

    Yields:
      {"status": "polling", "attempt": N, "max_attempts": 40}
      {"status": "success", "credentials": {...}}
      {"status": "error", "error": "..."}
    """
    params = {
        "grant_type": CODEX_GRANT_TYPE_DEVICE,
        "device_code": device_code,
        "client_id": CODEX_CLIENT_ID,
    }
    data = urllib.parse.urlencode(params).encode()
    max_attempts = 40  # 40 * 5 = 200s max

    for attempt in range(max_attempts):
        yield {"status": "polling", "attempt": attempt + 1, "max_attempts": max_attempts}

        req = urllib.request.Request(CODEX_TOKEN_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                token_data = json.loads(resp.read().decode())
                # Save to auth.json credential_pool
                _save_codex_credentials(token_data)
                yield {"status": "success", "credentials": {
                    "access_token": "***",
                    "refresh_token": "***",
                    "token_type": token_data.get("token_type"),
                    "expires_in": token_data.get("expires_in"),
                }}
                return
        except urllib.error.HTTPError as e:
            body = e.read().decode()
            try:
                err_data = json.loads(body)
                error = err_data.get("error", "")
                if error == "authorization_pending":
                    time.sleep(interval)
                    continue
                elif error == "slow_down":
                    time.sleep(interval + 5)
                    continue
                elif error == "expired_token":
                    yield {"status": "error", "error": "Device code expired. Please try again."}
                    return
                else:
                    yield {"status": "error", "error": err_data.get("error_description", error)}
                    return
            except Exception:
                yield {"status": "error", "error": body[:200]}
                return
        except Exception as e:
            yield {"status": "error", "error": str(e)}
            return

    yield {"status": "error", "error": "OAuth flow timed out. Please try again."}


def _save_codex_credentials(token_data):
    """Save Codex OAuth credentials to auth.json credential_pool."""
    auth = _read_auth_json()
    if "credential_pool" not in auth:
        auth["credential_pool"] = {}
    pool = auth["credential_pool"]

    if "openai-codex" not in pool:
        pool["openai-codex"] = []

    # Check if an oauth_device entry already exists (update in place)
    updated = False
    _now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for entry in pool["openai-codex"]:
        if entry.get("source") == "oauth_device":
            entry["access_token"] = token_data.get("access_token", "")
            entry["refresh_token"] = token_data.get("refresh_token", "")
            entry["auth_type"] = "oauth"
            entry["updated_at"] = _now_iso
            updated = True
            break

    if not updated:
        existing_ids = {e["id"] for e in pool.get("openai-codex", [])}
        for _ in range(3):  # retry on collision
            cred_id = "codex-oauth-" + uuid.uuid4().hex[:8]
            if cred_id not in existing_ids:
                break
        pool["openai-codex"].append({
            "id": cred_id,
            "label": "Codex OAuth",
            "auth_type": "oauth",
            "source": "oauth_device",
            "access_token": token_data.get("access_token", ""),
            "refresh_token": token_data.get("refresh_token", ""),
            "priority": 1,
            "created_at": _now_iso,
        })

    auth["updated_at"] = _now_iso
    _write_auth_json(auth)
