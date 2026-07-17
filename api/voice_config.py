"""Read/write the self-hosted STT & TTS voice endpoints from the WebUI.

Exposes the ``stt`` / ``tts`` OpenAI-compatible settings (base_url, api_key,
model, voice, …) so the Settings UI can display and edit them, matching the
category layout other tools (e.g. OpenWebUI) offer.

Source of truth is the active profile's ``config.yaml`` — the same file the
agent's STT path and the WebUI's ``/api/tts`` proxy already read. Writes are
comment-preserving (ruamel round-trip) and only touch the ``stt`` / ``tts``
subtrees.

Security:
  * GET redacts secrets — it returns ``api_key_set: bool``, never the key.
  * POST requires the normal WebUI auth AND an explicit opt-in
    (``HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE``), because letting the browser
    rewrite server config is a powerful capability. Off by default → 403.
"""

from __future__ import annotations

import os
from pathlib import Path

from api.helpers import j, read_body


# Fields we surface/accept per section. Anything else in config.yaml is left
# untouched by writes.
_STT_STR_FIELDS = ("provider", "base_url", "model", "response_format", "language", "mime_types")
_TTS_STR_FIELDS = ("provider", "base_url", "model", "voice", "response_format")

_MAX_STR = 2048


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _voice_config_writable() -> bool:
    """POST is enabled only when the operator opts in."""
    return _truthy("HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE")


def _auth_ok(handler) -> bool:
    from api.auth import is_auth_enabled, parse_cookie, verify_session

    if not is_auth_enabled():
        return True
    cv = parse_cookie(handler)
    return bool(cv and verify_session(cv))


def _openai_subblock(section: dict) -> dict:
    oai = section.get("openai") if isinstance(section, dict) else None
    return oai if isinstance(oai, dict) else {}


def _redacted_section(section: dict, str_fields, *, is_tts: bool) -> dict:
    section = section if isinstance(section, dict) else {}
    oai = _openai_subblock(section)
    out: dict = {}
    # Top-level provider selects the engine ("openai" == self-hosted path).
    out["provider"] = str(section.get("provider") or "").strip()
    if not is_tts:
        out["enabled"] = bool(section.get("enabled", True))
        out["mime_types"] = str(section.get("mime_types") or "").strip()
    for field in ("base_url", "model", "voice", "response_format", "language"):
        if field in str_fields or field in ("base_url", "model", "voice", "response_format", "language"):
            val = oai.get(field)
            if val is not None:
                out[field] = str(val)
    if is_tts:
        extra = section.get("extra_params")
        out["extra_params"] = extra if isinstance(extra, dict) else {}
    out["api_key_set"] = bool(str(oai.get("api_key") or "").strip())
    return out


def handle_voice_config_get(handler):
    """Return the redacted STT/TTS voice config for the Settings UI."""
    if not _auth_ok(handler):
        return j(handler, {"error": "unauthorized"}, status=401)
    try:
        from api.config import get_config

        cfg = get_config() or {}
        stt = cfg.get("stt", {})
        tts = cfg.get("tts", {})
        return j(handler, {
            "ok": True,
            "writable": _voice_config_writable(),
            "stt": _redacted_section(stt, _STT_STR_FIELDS, is_tts=False),
            "tts": _redacted_section(tts, _TTS_STR_FIELDS, is_tts=True),
        })
    except Exception:
        import traceback
        print("[webui] voice_config get error: " + traceback.format_exc(), flush=True)
        return j(handler, {"error": "failed to read voice config"}, status=500)


def _clean_str(val, *, lower: bool = False) -> str | None:
    if val is None:
        return None
    text = str(val).strip()
    if len(text) > _MAX_STR:
        raise ValueError("value too long")
    return text.lower() if lower else text


def _apply_section(dst, payload: dict, str_fields, *, is_tts: bool):
    """Mutate the ruamel mapping ``dst`` (config.yaml ``stt``/``tts`` block)
    from ``payload``. Only known fields are touched; an empty/absent api_key
    leaves any existing key in place."""
    from urllib.parse import urlsplit

    # Top-level provider + section-level fields.
    if "provider" in payload:
        prov = _clean_str(payload.get("provider"))
        if prov:
            dst["provider"] = prov
    if not is_tts and "enabled" in payload:
        dst["enabled"] = bool(payload.get("enabled"))
    if not is_tts and "mime_types" in payload:
        mt = _clean_str(payload.get("mime_types"))
        dst["mime_types"] = mt or ""
    if is_tts and "extra_params" in payload:
        extra = payload.get("extra_params")
        if extra in (None, ""):
            extra = {}
        if not isinstance(extra, dict):
            raise ValueError("extra_params must be an object")
        dst["extra_params"] = extra

    # openai sub-block.
    oai = dst.get("openai")
    if not isinstance(oai, dict):
        oai = {}
        dst["openai"] = oai

    for field in ("base_url", "model", "voice", "response_format", "language"):
        if field not in payload:
            continue
        val = _clean_str(payload.get(field))
        if field == "base_url" and val:
            parsed = urlsplit(val)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("base_url must be an http(s) URL")
        oai[field] = val or ""

    # api_key: only overwrite when a non-empty value is supplied, so the
    # redacted GET → POST round-trip never wipes a stored key.
    if "api_key" in payload:
        key = _clean_str(payload.get("api_key"))
        if key:
            oai["api_key"] = key


def handle_voice_config_post(handler):
    """Write STT/TTS voice endpoints to config.yaml (auth + opt-in gated)."""
    if handler.command != "POST":
        return j(handler, {"error": "POST required"}, status=405)
    if not _auth_ok(handler):
        return j(handler, {"error": "unauthorized"}, status=401)
    if not _voice_config_writable():
        return j(handler, {
            "error": "voice config is read-only; set HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE=1 to enable editing",
        }, status=403)

    try:
        body = read_body(handler) or {}
    except Exception:
        return j(handler, {"error": "invalid request body"}, status=400)

    stt_in = body.get("stt") if isinstance(body.get("stt"), dict) else None
    tts_in = body.get("tts") if isinstance(body.get("tts"), dict) else None
    if stt_in is None and tts_in is None:
        return j(handler, {"error": "no stt or tts section in body"}, status=400)

    load_yaml, dump_yaml, _preserves_comments = _yaml_io()

    try:
        from api.config import _get_config_path, reload_config_if_stale

        config_path = Path(_get_config_path())
        if config_path.exists():
            data = load_yaml(config_path.read_text(encoding="utf-8"))
            if data is None:
                data = {}
        else:
            data = {}

        if stt_in is not None:
            sec = data.get("stt")
            if not isinstance(sec, dict):
                sec = {}
                data["stt"] = sec
            _apply_section(sec, stt_in, _STT_STR_FIELDS, is_tts=False)
        if tts_in is not None:
            sec = data.get("tts")
            if not isinstance(sec, dict):
                sec = {}
                data["tts"] = sec
            _apply_section(sec, tts_in, _TTS_STR_FIELDS, is_tts=True)

        # Timestamped backup, then atomic replace.
        _write_config_atomic(config_path, dump_yaml, data)

        reload_config_if_stale()
        from api.config import get_config
        cfg = get_config() or {}
        return j(handler, {
            "ok": True,
            "writable": True,
            "stt": _redacted_section(cfg.get("stt", {}), _STT_STR_FIELDS, is_tts=False),
            "tts": _redacted_section(cfg.get("tts", {}), _TTS_STR_FIELDS, is_tts=True),
        })
    except ValueError as e:
        return j(handler, {"error": str(e)}, status=400)
    except Exception:
        import traceback
        print("[webui] voice_config post error: " + traceback.format_exc(), flush=True)
        return j(handler, {"error": "failed to write voice config"}, status=500)


def _yaml_io():
    """Return ``(load, dump, preserves_comments)``.

    Prefer ruamel (round-trip → hand-written comments/formatting survive an
    edit); fall back to PyYAML so the feature still works where ruamel isn't
    installed, at the cost of dropping comments. The live WebUI runs on the
    agent venv, which ships ruamel, so comments are preserved in practice.
    """
    try:
        from ruamel.yaml import YAML
        import io

        y = YAML()
        y.preserve_quotes = True

        def _load(text):
            return y.load(text) if (text or "").strip() else {}

        def _dump(data):
            buf = io.StringIO()
            y.dump(data, buf)
            return buf.getvalue()

        return _load, _dump, True
    except Exception:
        import yaml as _yaml

        def _load(text):
            return (_yaml.safe_load(text) or {}) if (text or "").strip() else {}

        def _dump(data):
            return _yaml.safe_dump(data, sort_keys=False, allow_unicode=True)

        return _load, _dump, False


def _write_config_atomic(config_path: Path, dump_yaml, data) -> None:
    """Back up the existing config.yaml then atomically replace it."""
    import tempfile

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        try:
            backup = config_path.with_suffix(
                config_path.suffix + ".voicebak-" + str(int(config_path.stat().st_mtime))
            )
            if not backup.exists():
                backup.write_bytes(config_path.read_bytes())
        except Exception:
            # A failed backup must not block the write, but log it.
            print("[webui] voice_config: backup of config.yaml failed", flush=True)

    text = dump_yaml(data)

    fd, tmp_name = tempfile.mkstemp(
        prefix=".config-voice-", suffix=".yaml", dir=str(config_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, config_path)
    finally:
        try:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        except Exception:
            pass


def _tts_provider_capability():
    """Return (available, provider) for the configured TTS engine.

    Mirrors the STT capability probe: the frontend uses this to decide whether
    the hands-free voice-mode's speak leg has a working server engine. Cheap and
    side-effect free.
    """
    try:
        from api.config import get_config

        cfg = get_config() or {}
        tts = cfg.get("tts", {})
        tts = tts if isinstance(tts, dict) else {}
        provider = str(tts.get("provider") or "").strip() or "edge"
    except Exception:
        return False, "none"

    try:
        import tools.tts_tool as tts_tool

        check = getattr(tts_tool, "check_tts_requirements", None)
        if callable(check):
            return bool(check()), provider
    except Exception:
        pass
    # edge is the built-in default and needs no config to be usable.
    return provider == "edge", provider


def handle_tts_capability(handler):
    available, provider = _tts_provider_capability()
    return j(handler, {"ok": True, "available": bool(available), "provider": provider})
