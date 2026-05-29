"""Voice transcription module backend (multi-model, OpenAI-compatible).

Powers the dedicated "Voice" sidebar module and its Settings section. It is
independent of the composer dictation button, which delegates to the agent's
``tools.transcription_tools`` in the Hermes agent venv.

This module forwards captured browser audio to one of several user-configured
OpenAI-compatible speech endpoints (``POST {base_url}/audio/transcriptions``),
such as a local server hosting ``ibm-granite/granite-speech-3.3-8b``. Users
register endpoints in Settings → Voice and switch between them on the fly from
the Voice panel. Configuration is persisted in ``voice_config.json`` in the
WebUI state directory; transcript history lives in ``voice_transcripts.json``.

No heavy dependencies: audio is forwarded with stdlib ``urllib`` + a
hand-rolled multipart body, keeping the WebUI's minimal-deps contract intact.

Environment fallback (back-compat / zero-config bootstrap): when no models are
configured but ``GRANITE_STT_BASE_URL`` is set, a read-only "env" model is
synthesized from these variables so an env-only deployment still works:

    GRANITE_STT_BASE_URL   OpenAI-compatible base URL, e.g. http://localhost:8000/v1
    GRANITE_STT_MODEL      Model id (default: granite-speech-3.3-8b)
    GRANITE_STT_API_KEY    Optional bearer token
    GRANITE_STT_TIMEOUT    Request timeout seconds (default 120)

See docs/voice-transcription.md for the full contract.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from api.config import STATE_DIR

# ── Files ────────────────────────────────────────────────────────────────────
VOICE_CONFIG_FILE = STATE_DIR / "voice_config.json"
VOICE_TRANSCRIPTS_FILE = STATE_DIR / "voice_transcripts.json"

_CONFIG_LOCK = threading.Lock()
_HISTORY_LOCK = threading.Lock()
_MAX_HISTORY = 100
_MAX_MODELS = 25
_ENV_MODEL_ID = "env-granite"
_DEFAULT_MODEL_NAME = "granite-speech-3.3-8b"
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,48}$")


# ── Env fallback ─────────────────────────────────────────────────────────────
def _env_model() -> dict | None:
    """Synthesize a read-only model entry from GRANITE_STT_* env vars, or None."""
    base_url = (os.getenv("GRANITE_STT_BASE_URL") or "").strip().rstrip("/")
    if not base_url:
        return None
    try:
        timeout = int(float(os.getenv("GRANITE_STT_TIMEOUT") or 120))
    except ValueError:
        timeout = 120
    return {
        "id": _ENV_MODEL_ID,
        "label": "Granite (env)",
        "base_url": base_url,
        "model": (os.getenv("GRANITE_STT_MODEL") or _DEFAULT_MODEL_NAME).strip(),
        "api_key": (os.getenv("GRANITE_STT_API_KEY") or "").strip(),
        "timeout": timeout,
        "source": "env",
    }


# ── Config persistence ───────────────────────────────────────────────────────
def _read_config_file() -> dict:
    if not VOICE_CONFIG_FILE.exists():
        return {"models": [], "active_id": ""}
    try:
        data = json.loads(VOICE_CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"models": [], "active_id": ""}
    if not isinstance(data, dict):
        return {"models": [], "active_id": ""}
    models = data.get("models")
    if not isinstance(models, list):
        models = []
    return {"models": models, "active_id": str(data.get("active_id") or "")}


def load_voice_config() -> dict:
    """Return the full resolved config including any env-fallback model.

    Shape: ``{"models": [<model>...], "active_id": str}``. Stored models keep
    their raw ``api_key``; the env model (if any) is appended unless an id
    collision exists. ``active_id`` is normalized to a present model id.
    """
    cfg = _read_config_file()
    models = [m for m in cfg["models"] if isinstance(m, dict) and m.get("id")]
    ids = {m["id"] for m in models}
    env = _env_model()
    if env and env["id"] not in ids:
        models.append(env)
        ids.add(env["id"])
    active = cfg["active_id"]
    if active not in ids:
        active = models[0]["id"] if models else ""
    return {"models": models, "active_id": active}


def _public_model(m: dict) -> dict:
    """Strip secrets for client responses."""
    return {
        "id": m.get("id", ""),
        "label": m.get("label") or m.get("id", ""),
        "base_url": m.get("base_url", ""),
        "model": m.get("model", ""),
        "timeout": int(m.get("timeout") or 120),
        "has_api_key": bool((m.get("api_key") or "").strip()),
        "source": m.get("source", "user"),
    }


def public_config() -> dict:
    """Client-safe config: api keys masked, active id resolved."""
    cfg = load_voice_config()
    return {
        "models": [_public_model(m) for m in cfg["models"]],
        "active_id": cfg["active_id"],
        "configured": bool(cfg["models"]),
    }


def is_configured() -> bool:
    return bool(load_voice_config()["models"])


def _validate_incoming_model(raw: dict, existing_by_id: dict) -> dict:
    """Validate one incoming model dict, preserving an unchanged api key.

    Raises ValueError on invalid input. ``api_key`` is preserved from the
    existing entry when the incoming dict omits the key (client did not edit
    it); an explicit empty string clears it.
    """
    if not isinstance(raw, dict):
        raise ValueError("Each model must be an object")
    mid = str(raw.get("id") or "").strip().lower()
    if not _ID_RE.match(mid):
        raise ValueError(f"Invalid model id: {raw.get('id')!r} (use a-z, 0-9, -, _)")
    if mid == _ENV_MODEL_ID:
        raise ValueError("'env-granite' is reserved for the environment fallback")
    base_url = str(raw.get("base_url") or "").strip().rstrip("/")
    if not re.match(r"^https?://", base_url):
        raise ValueError(f"base_url must start with http:// or https:// (model {mid})")
    model = str(raw.get("model") or "").strip()
    if not model:
        raise ValueError(f"model is required (model {mid})")
    label = str(raw.get("label") or mid).strip()[:80]
    try:
        timeout = int(raw.get("timeout") or 120)
    except (TypeError, ValueError):
        timeout = 120
    timeout = max(1, min(timeout, 600))
    if "api_key" in raw and raw.get("api_key") is not None:
        api_key = str(raw.get("api_key"))
    else:
        api_key = existing_by_id.get(mid, {}).get("api_key", "")
    return {
        "id": mid,
        "label": label,
        "base_url": base_url,
        "model": model,
        "api_key": api_key,
        "timeout": timeout,
        "source": "user",
    }


def save_voice_config(models: list, active_id: str | None = None) -> dict:
    """Validate and persist the user model list. Returns the public config.

    The env-fallback model is never persisted. ``active_id`` is clamped to a
    present id (env model included as a valid target).
    """
    if not isinstance(models, list):
        raise ValueError("models must be a list")
    if len(models) > _MAX_MODELS:
        raise ValueError(f"Too many models (max {_MAX_MODELS})")
    with _CONFIG_LOCK:
        existing_by_id = {
            m["id"]: m for m in _read_config_file()["models"]
            if isinstance(m, dict) and m.get("id")
        }
        cleaned: list[dict] = []
        seen: set[str] = set()
        for raw in models:
            entry = _validate_incoming_model(raw, existing_by_id)
            if entry["id"] in seen:
                raise ValueError(f"Duplicate model id: {entry['id']}")
            seen.add(entry["id"])
            cleaned.append(entry)
        # Resolve active against persisted ids + env model id.
        valid_ids = set(seen)
        if _env_model():
            valid_ids.add(_ENV_MODEL_ID)
        active = str(active_id or "").strip()
        if active not in valid_ids:
            active = cleaned[0]["id"] if cleaned else (_ENV_MODEL_ID if _env_model() else "")
        VOICE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        VOICE_CONFIG_FILE.write_text(
            json.dumps({"models": cleaned, "active_id": active}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return public_config()


def set_active_model(model_id: str) -> dict:
    """Switch the active model on the fly. Returns the public config."""
    cfg = _read_config_file()
    ids = {m["id"] for m in cfg["models"] if isinstance(m, dict) and m.get("id")}
    if _env_model():
        ids.add(_ENV_MODEL_ID)
    model_id = str(model_id or "").strip()
    if model_id not in ids:
        raise ValueError(f"Unknown model id: {model_id}")
    with _CONFIG_LOCK:
        cfg = _read_config_file()
        VOICE_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        VOICE_CONFIG_FILE.write_text(
            json.dumps({"models": cfg["models"], "active_id": model_id}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return public_config()


def get_model(model_id: str | None = None) -> dict | None:
    """Resolve a model entry by id, falling back to the active model."""
    cfg = load_voice_config()
    target = (model_id or "").strip() or cfg["active_id"]
    for m in cfg["models"]:
        if m.get("id") == target:
            return m
    return None


# ── Transcription ────────────────────────────────────────────────────────────
def _build_multipart(file_bytes: bytes, filename: str, content_type: str, model: str) -> tuple[bytes, str]:
    """Encode an OpenAI-style /audio/transcriptions multipart body."""
    boundary = "----hermesvoice" + uuid.uuid4().hex
    crlf = b"\r\n"
    parts: list[bytes] = []

    def _field(name: str, value: str) -> None:
        parts.append(b"--" + boundary.encode())
        parts.append(b'Content-Disposition: form-data; name="' + name.encode() + b'"')
        parts.append(b"")
        parts.append(value.encode())

    _field("model", model)
    _field("response_format", "json")
    parts.append(b"--" + boundary.encode())
    parts.append(
        b'Content-Disposition: form-data; name="file"; filename="' + filename.encode() + b'"'
    )
    parts.append(b"Content-Type: " + (content_type or "application/octet-stream").encode())
    parts.append(b"")
    parts.append(file_bytes)
    parts.append(b"--" + boundary.encode() + b"--")
    parts.append(b"")
    return crlf.join(parts), boundary


def _post_transcription(entry: dict, file_bytes: bytes, filename: str, content_type: str) -> dict:
    url = entry["base_url"] + "/audio/transcriptions"
    body, boundary = _build_multipart(file_bytes, filename, content_type, entry["model"])
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    api_key = (entry.get("api_key") or "").strip()
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=int(entry.get("timeout") or 120)) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        return {"success": False, "error": f"Transcription endpoint error {exc.code}: {detail or exc.reason}"}
    except urllib.error.URLError as exc:
        return {"success": False, "error": f"Could not reach transcription endpoint: {exc.reason}"}
    except Exception as exc:  # pragma: no cover - defensive
        return {"success": False, "error": f"Transcription failed: {exc}"}

    try:
        data = json.loads(raw)
    except ValueError:
        transcript = raw.strip()
        return (
            {"success": True, "transcript": transcript}
            if transcript
            else {"success": False, "error": "Empty transcription response"}
        )
    transcript = str(data.get("text") or data.get("transcript") or "").strip()
    if not transcript:
        return {"success": False, "error": "Empty transcription response"}
    return {"success": True, "transcript": transcript}


def transcribe(file_bytes: bytes, filename: str, content_type: str, model_id: str | None = None) -> dict:
    """Transcribe audio with the requested (or active) model.

    Returns ``{"success": True, "transcript": str, "model_id": str}`` or
    ``{"success": False, "error": str}``.
    """
    cfg = load_voice_config()
    if not cfg["models"]:
        return {
            "success": False,
            "error": "No transcription models configured. Add one in Settings → Voice.",
        }
    entry = get_model(model_id)
    if entry is None:
        return {"success": False, "error": f"Transcription model not found: {model_id or cfg['active_id']}"}
    result = _post_transcription(entry, file_bytes, filename, content_type)
    if result.get("success"):
        result["model_id"] = entry["id"]
    return result


# ── History CRUD ─────────────────────────────────────────────────────────────
def load_transcripts() -> list:
    if not VOICE_TRANSCRIPTS_FILE.exists():
        return []
    try:
        data = json.loads(VOICE_TRANSCRIPTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _save_transcripts(items: list) -> None:
    VOICE_TRANSCRIPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    VOICE_TRANSCRIPTS_FILE.write_text(
        json.dumps(items[:_MAX_HISTORY], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def add_transcript(text: str, model_id: str | None = None) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    entry = {"id": uuid.uuid4().hex[:12], "text": text, "created_at": time.time()}
    if model_id:
        entry["model_id"] = model_id
    with _HISTORY_LOCK:
        items = load_transcripts()
        items.insert(0, entry)
        _save_transcripts(items)
    return entry


def delete_transcript(entry_id: str) -> bool:
    with _HISTORY_LOCK:
        items = load_transcripts()
        kept = [it for it in items if it.get("id") != entry_id]
        if len(kept) == len(items):
            return False
        _save_transcripts(kept)
        return True


def clear_transcripts() -> None:
    with _HISTORY_LOCK:
        _save_transcripts([])
