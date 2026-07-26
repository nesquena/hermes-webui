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
#
# The STT list deliberately stops at what the agent's built-in OpenAI STT path
# actually consumes (``stt.openai.base_url`` / ``api_key`` / ``model``, plus the
# section-level ``provider``/``enabled``/``mime_types``).
# ``language``/``request_format``/``response_format`` were exposed here and in
# Settings but are read by nothing in ``tools/transcription_tools.py`` for a
# built-in provider — ``_transcribe_openai`` hardcodes its response format, and
# ``language`` is only forwarded for PLUGIN providers. A control that silently
# does nothing is worse than an absent one: the operator sets a language, hears
# the wrong one back, and has no way to tell the setting was ignored. The live
# language hint is the per-request ``language`` field on ``/api/transcribe``,
# which is feature-detected against the installed agent.
_STT_STR_FIELDS = ("provider", "base_url", "model", "mime_types")
_TTS_STR_FIELDS = ("provider", "base_url", "model", "voice", "response_format")

_MAX_STR = 2048


def _voice_config_writable() -> bool:
    """POST is enabled only when the operator opts in.

    Read from the immutable startup snapshot, never from the live environment:
    a profile's dotenv file is projected into ``os.environ`` process-wide, so a
    live read would let any profile grant itself server-config write authority
    — and leak that grant into concurrent requests for other profiles.
    """
    from api.operator_env import operator_env_truthy

    return operator_env_truthy("HERMES_WEBUI_ALLOW_VOICE_CONFIG_WRITE")


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
    # openai-sub-block fields; section-level ones (provider, enabled,
    # mime_types) are handled above.
    for field in str_fields:
        if field in ("provider", "enabled", "mime_types"):
            continue
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


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _base_url_host(value) -> str:
    """Return a comparable ``scheme://host:port`` for an endpoint URL.

    An explicitly written default port is normalized away, so editing
    ``https://tts.example/v1`` to ``https://tts.example:443/v1`` is not read as
    repointing at a different server (which would drop the stored key for no
    reason). Path and trailing slash are ignored — they do not change WHO the
    credential is being sent to, which is the only question this answers.
    """
    from urllib.parse import urlsplit

    try:
        parsed = urlsplit(str(value or "").strip())
    except Exception:
        return ""
    if not parsed.netloc:
        return ""
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError:
        # Unparseable port — compare the raw netloc rather than guessing.
        return scheme + "://" + parsed.netloc.lower()
    if port is None or port == _DEFAULT_PORTS.get(scheme):
        return scheme + "://" + host
    return f"{scheme}://{host}:{port}"


def _apply_section(dst, payload: dict, str_fields, *, is_tts: bool) -> dict:
    """Mutate the ruamel mapping ``dst`` (config.yaml ``stt``/``tts`` block)
    from ``payload``.

    Only known fields are touched. Credential semantics:

    * a non-empty ``api_key`` replaces the stored one;
    * ``api_key_clear: true`` deletes it (there was previously NO way to
      remove a stored key from the UI at all — the redacted GET returns a
      boolean, so re-saving with a blank field just kept the old secret
      forever);
    * an absent/blank ``api_key`` keeps the stored one, EXCEPT when this save
      also repoints ``base_url`` at a different host. A key issued for the old
      endpoint is not a credential for the new one, and carrying it over means
      the next request ships that secret — in cleartext for a plain-http LAN
      target — to a server the operator never gave it to. On a host change
      without a new key we drop it and report that in the response.

    Returns a dict of notes for the caller (currently ``{"api_key_dropped":
    bool}``).
    """
    from urllib.parse import urlsplit

    notes = {"api_key_dropped": False}

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

    previous_host = _base_url_host(oai.get("base_url"))
    host_changed = False

    allowed = tuple(f for f in str_fields if f not in ("provider", "enabled", "mime_types"))
    for field in allowed:
        if field not in payload:
            continue
        val = _clean_str(payload.get(field))
        if field == "base_url" and val:
            parsed = urlsplit(val)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise ValueError("base_url must be an http(s) URL")
        if field == "base_url":
            host_changed = _base_url_host(val) != previous_host
        oai[field] = val or ""

    # api_key: see the docstring. Explicit clear wins over everything.
    new_key = _clean_str(payload.get("api_key")) if "api_key" in payload else None
    if payload.get("api_key_clear"):
        oai.pop("api_key", None)
    elif new_key:
        oai["api_key"] = new_key
    elif host_changed and str(oai.get("api_key") or "").strip():
        oai.pop("api_key", None)
        notes["api_key_dropped"] = True

    return notes


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
        from api.config import _cfg_lock, _get_config_path, reload_config_if_stale

        config_path = Path(_get_config_path())
        notes = {}
        # The whole read-modify-write runs under the SAME lock every other
        # config.yaml writer in api/config.py holds. Without it, two
        # acknowledged saves (voice endpoints in one tab, the model picker in
        # another) each read the pre-write file and the second `os.replace`
        # silently discards the first — both requests answered 200. The lock is
        # not reentrant and `reload_config_if_stale()` takes it itself, so the
        # reload happens after the block.
        with _cfg_lock:
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
                notes["stt"] = _apply_section(sec, stt_in, _STT_STR_FIELDS, is_tts=False)
            if tts_in is not None:
                sec = data.get("tts")
                if not isinstance(sec, dict):
                    sec = {}
                    data["tts"] = sec
                notes["tts"] = _apply_section(sec, tts_in, _TTS_STR_FIELDS, is_tts=True)

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
            "api_key_dropped": {
                section: bool(note.get("api_key_dropped"))
                for section, note in notes.items()
            },
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


# How many timestamped backups of config.yaml to keep per profile. Each one is
# a full copy of a file that can hold API keys, so they are not left to
# accumulate indefinitely.
_MAX_CONFIG_BACKUPS = 5
_BACKUP_SUFFIX = ".voicebak-"


def _backup_config(config_path: Path) -> None:
    """Copy config.yaml aside before it is rewritten.

    The backup inherits the SOURCE FILE'S mode and is created with it from the
    start (``O_CREAT|O_EXCL`` at 0600, then chmod). The previous version used
    ``Path.write_bytes``, which applies the process umask: a deliberately
    hardened ``0600`` config.yaml produced a **0644 backup sitting next to it,
    containing the same API key** — every local account could read the secret
    the operator had locked down. Creating it 0600-then-narrowing also means
    there is no window in which the file exists with looser bits.

    Backups are pruned to the newest ``_MAX_CONFIG_BACKUPS``.
    """
    try:
        st = config_path.stat()
    except OSError:
        return
    source_mode = st.st_mode & 0o777
    # mtime_ns so rapid successive writes each keep their backup.
    backup = config_path.with_suffix(config_path.suffix + _BACKUP_SUFFIX + str(st.st_mtime_ns))
    if backup.exists():
        return
    payload = config_path.read_bytes()
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        if fd >= 0:
            os.close(fd)
    if source_mode != 0o600:
        os.chmod(backup, source_mode)
    _prune_config_backups(config_path)


def _prune_config_backups(config_path: Path) -> None:
    """Keep only the newest ``_MAX_CONFIG_BACKUPS`` copies."""
    try:
        siblings = sorted(
            config_path.parent.glob(config_path.name + _BACKUP_SUFFIX + "*"),
            key=lambda p: p.name,
        )
    except OSError:
        return
    for stale in siblings[:-_MAX_CONFIG_BACKUPS] if len(siblings) > _MAX_CONFIG_BACKUPS else []:
        try:
            stale.unlink()
        except OSError:
            pass


def _write_config_atomic(config_path: Path, dump_yaml, data) -> None:
    """Back up the existing config.yaml then atomically replace it.

    The replace itself goes through ``api.paths._atomic_write_text`` — the same
    writer every other config.yaml save uses. Rolling our own
    ``mkstemp``+``os.replace`` here got the interesting cases wrong: a
    **symlinked config.yaml was replaced by a regular file**, so an operator who
    points ``~/.hermes/config.yaml`` at a file in a managed dotfiles repo had
    the link silently severed and their real config left un-updated. The shared
    writer follows the symlink to its referent, re-verifies the target hasn't
    moved before committing, preserves uid/gid and POSIX ACLs, and falls back to
    a guarded in-place write where an atomic replace is impossible.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        try:
            _backup_config(config_path)
        except Exception:
            # A failed backup must not block the write, but log it.
            print("[webui] voice_config: backup of config.yaml failed", flush=True)

    from api import paths as _paths

    _paths._atomic_write_text(config_path, dump_yaml(data), encoding="utf-8")

    # The parsed-config memo is keyed on (mtime_ns, size); evict our own write so
    # the reload that follows re-parses the bytes we just committed.
    try:
        from api.config import _yaml_file_cache, _yaml_file_cache_lock

        with _yaml_file_cache_lock:
            _yaml_file_cache.pop(str(config_path), None)
    except Exception:
        pass


def _tts_provider_capability():
    """Return (available, provider, engines) for the TTS speaking leg.

    Answers with the SAME resolution ``/api/tts`` uses, via
    ``api.routes.tts_engine_available``. This used to delegate to an optional
    Agent-side requirements helper while the route ran the WebUI's own
    implementations — so the probe could report a working leg the route then
    refused with 503, and vice versa. The client gates its speaking leg on this
    answer, so a disagreement is a broken feature either way.

    ``engines`` is a per-engine map, so a client whose selected engine differs
    from the configured default can ask about the one it will actually call
    instead of inferring from a single boolean.
    """
    try:
        from api.config import get_config

        cfg = get_config() or {}
        tts = cfg.get("tts", {})
        tts = tts if isinstance(tts, dict) else {}
        provider = str(tts.get("provider") or "").strip() or "edge"
    except Exception:
        return False, "none", {}

    try:
        from api.routes import tts_engine_available
        from api.upload import _agent_profile_scope

        with _agent_profile_scope("/api/tts/capability"):
            engines = {
                name: bool(tts_engine_available(name))
                for name in ("edge", "openai", "elevenlabs")
            }
    except Exception:
        return False, provider, {}
    return bool(engines.get(provider, False)), provider, engines


def handle_tts_capability(handler):
    available, provider, engines = _tts_provider_capability()
    return j(handler, {
        "ok": True,
        "available": bool(available),
        "provider": provider,
        "engines": engines,
    })
