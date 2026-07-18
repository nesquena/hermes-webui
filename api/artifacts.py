"""Hermes Web UI -- published artifacts with stable, versioned URLs.

An artifact is a file the agent produced (an HTML report, a chart, a PDF)
that the user explicitly publishes so it stays reachable under a stable URL:

    /artifact/<token>          -> newest version
    /artifact/<token>?v=N      -> pinned version

Re-publishing the same source file appends a new version under the SAME token,
so a link shared once keeps pointing at the freshest state (Claude-style
artifact semantics). Storage is an immutable copy under STATE_DIR/artifacts --
serving never touches the live workspace file, so later edits or deletions of
the source cannot change what an already-shared link exposes.

Access model (mirrors api/shares.py):
  - publish / revoke / list require an authenticated session (enforced in the
    routes layer; the endpoints live under /api/).
  - GET /artifact/<token> is reachable without auth ONLY for artifacts
    explicitly published with public=true. Non-public artifacts require a valid
    session cookie and return 404 (not 401) to anonymous callers so tokens
    cannot be probed.
  - Public HTML/text artifacts pass through the ALWAYS-ON credential redactor
    (same hard boundary as public share snapshots).

The feature as a whole is opt-in: settings["artifacts_enabled"], default off,
env override HERMES_WEBUI_ARTIFACTS (generic deployment; nothing TARS-specific).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from pathlib import Path

from api.config import STATE_DIR, load_settings
from api.helpers import _redact_fn_cached as _force_redact_credentials

logger = logging.getLogger(__name__)

ARTIFACTS_DIR = STATE_DIR / "artifacts"
_ARTIFACTS_LOCK = threading.Lock()

# Hard cap per published file. Artifacts are chat deliverables (reports,
# charts, small bundles), not a file-hosting service.
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

# Basenames that must never be publishable even when they appear inside an
# allowed root. Mirrors the /api/media #3234 deny set: Hermes state + secrets.
_DENY_FILENAMES = {
    "settings.json", "state.db", "state.db-wal", "state.db-shm",
    "auth.json", "auth.lock", "config.yaml", "config.yml", ".env",
    ".signing_key", ".pbkdf2_key", ".sessions.json",
    "google_token.json", "google_client_secret.json",
    "gateway_state.json", "channel_directory.json", "jobs.json",
    "passkeys.json", ".passkey_challenges.json", ".login_attempts.json",
}

# MIME map for serving artifact files. HTML is special-cased by the routes
# layer (sandbox CSP); everything unknown downloads as octet-stream.
_MIME_MAP = {
    ".html": "text/html", ".htm": "text/html",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".avif": "image/avif",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".txt": "text/plain", ".md": "text/plain", ".csv": "text/plain",
    ".json": "application/json",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".zip": "application/zip",
}

# Text-like MIME types that run through the credential redactor when the
# artifact is public. Binary formats pass through untouched.
_REDACTABLE_MIME = {"text/html", "text/plain", "application/json", "image/svg+xml"}

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


def artifacts_enabled() -> bool:
    try:
        return bool(load_settings().get("artifacts_enabled", False))
    except Exception:
        return False


def _artifact_dir(token: str) -> Path:
    token = str(token or "").strip()
    if not _TOKEN_RE.match(token):
        raise ValueError("invalid artifact token")
    return ARTIFACTS_DIR / token


def _meta_path(token: str) -> Path:
    return _artifact_dir(token) / "meta.json"


def _write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f"{path.stem}.", suffix=".tmp", text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _allowed_source_roots() -> list[Path]:
    """Roots a publishable file may live in: active workspace + /tmp.

    Deliberately narrower than /api/media's serving allowlist -- publishing is
    an explicit, durable act, so it only accepts the places agent deliverables
    legitimately land. Hermes home state directories are NOT publishable.
    """
    roots: list[Path] = [Path("/tmp").resolve()]
    try:
        from api.workspace import get_last_workspace
        ws = Path(get_last_workspace()).resolve()
        if ws.is_dir():
            roots.append(ws)
    except Exception:
        pass
    extra = os.environ.get("ARTIFACT_ALLOWED_ROOTS", "").strip()
    if extra:
        for root in extra.split(os.pathsep):
            root = root.strip()
            if root:
                try:
                    rp = Path(root).resolve()
                    if rp.is_dir():
                        roots.append(rp)
                except Exception:
                    pass
    return roots


def _path_is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def validate_source_path(raw_path: str) -> Path:
    """Resolve and validate a publish source path; raises ValueError with a
    user-facing message on every rejection."""
    raw_path = str(raw_path or "").strip()
    if not raw_path:
        raise ValueError("path is required")
    raw_path = os.path.expanduser(raw_path)
    try:
        target = Path(raw_path).resolve(strict=True)
    except FileNotFoundError:
        raise ValueError("file not found")
    except Exception:
        raise ValueError("invalid path")
    if not target.is_file():
        raise ValueError("path is not a regular file")
    if target.name.casefold() in {n.casefold() for n in _DENY_FILENAMES}:
        raise ValueError("this file type is not publishable")
    if not any(_path_is_within(target, r) for r in _allowed_source_roots()):
        raise ValueError("path is outside the publishable roots (workspace, /tmp)")
    size = target.stat().st_size
    if size == 0:
        raise ValueError("file is empty")
    if size > MAX_ARTIFACT_BYTES:
        raise ValueError("file exceeds the 50 MB artifact limit")
    return target


def mime_for(filename: str) -> str:
    return _MIME_MAP.get(Path(str(filename)).suffix.lower(), "application/octet-stream")


def _load_meta(token: str) -> dict | None:
    try:
        path = _meta_path(token)
    except ValueError:
        return None
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Failed to read artifact meta %s", path, exc_info=True)
        return None
    return meta if isinstance(meta, dict) else None


def _find_token_for_source(source: str) -> str | None:
    """Existing non-revoked artifact for this resolved source path, if any."""
    if not ARTIFACTS_DIR.is_dir():
        return None
    for meta_file in ARTIFACTS_DIR.glob("*/meta.json"):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict) or meta.get("revoked_at"):
            continue
        if meta.get("source_path") == source:
            return str(meta.get("token") or "") or None
    return None


def publish_artifact(
    raw_path: str,
    *,
    title: str | None = None,
    public: bool = False,
    session_id: str | None = None,
    token: str | None = None,
) -> dict:
    """Publish (or re-publish) a file as a new artifact version.

    Explicit ``token`` re-publishes that artifact; otherwise an existing
    non-revoked artifact for the same resolved source path is version-bumped;
    otherwise a fresh token is minted.
    """
    source = validate_source_path(raw_path)
    mime = mime_for(source.name)

    with _ARTIFACTS_LOCK:
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        if token:
            token = str(token).strip()
            meta = _load_meta(token)
            if meta is None or meta.get("revoked_at"):
                raise ValueError("unknown or revoked artifact token")
        else:
            token = _find_token_for_source(str(source))
            meta = _load_meta(token) if token else None
        if meta is None:
            token = secrets.token_urlsafe(18)
            meta = {
                "token": token,
                "source_path": str(source),
                "filename": source.name,
                "mime": mime,
                "title": "",
                "public": False,
                "session_id": str(session_id or ""),
                "created_at": time.time(),
                "updated_at": None,
                "revoked_at": None,
                "versions": [],
            }

        version = len(meta.get("versions") or []) + 1
        vdir = _artifact_dir(token) / f"v{version}"
        vdir.mkdir(parents=True, exist_ok=True)
        dest = vdir / source.name

        if public or bool(meta.get("public")):
            # Public boundary: text-like content is credential-redacted at
            # publish time (immutable copy), so a later toggle to public can
            # never resurrect unredacted bytes. Binary formats copy verbatim.
            if mime in _REDACTABLE_MIME:
                try:
                    text = source.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    raise ValueError(f"could not read file: {exc}")
                dest.write_text(_force_redact_credentials(text), encoding="utf-8")
            else:
                shutil.copyfile(source, dest)
        else:
            shutil.copyfile(source, dest)

        now = time.time()
        meta["versions"] = list(meta.get("versions") or []) + [{
            "v": version,
            "size": dest.stat().st_size,
            "created_at": now,
        }]
        meta["updated_at"] = now
        meta["mime"] = mime
        meta["filename"] = source.name
        meta["source_path"] = str(source)
        if title is not None and str(title).strip():
            meta["title"] = str(title).strip()[:200]
        elif not meta.get("title"):
            meta["title"] = source.name
        meta["public"] = bool(public)
        if session_id:
            meta["session_id"] = str(session_id)
        _write_json_atomic(_meta_path(token), meta)

    return {
        "token": token,
        "url": f"/artifact/{token}",
        "title": meta["title"],
        "version": version,
        "public": meta["public"],
        "mime": mime,
        "filename": meta["filename"],
        "created_at": meta["created_at"],
        "updated_at": meta["updated_at"],
    }


def resolve_artifact_file(token: str, version: int | None = None) -> tuple[dict, Path] | None:
    """(meta, file_path) for a live artifact version, or None."""
    meta = _load_meta(token)
    if meta is None or meta.get("revoked_at"):
        return None
    versions = meta.get("versions") or []
    if not versions:
        return None
    if version is None:
        version = int(versions[-1].get("v") or len(versions))
    else:
        version = int(version)
        if not any(int(v.get("v") or 0) == version for v in versions):
            return None
    try:
        vdir = _artifact_dir(str(meta.get("token") or token)) / f"v{version}"
    except ValueError:
        return None
    fname = str(meta.get("filename") or "")
    fpath = (vdir / fname) if fname else None
    if not fpath or not fpath.is_file():
        return None
    # Belt-and-braces: the served file must stay inside this artifact's dir.
    if not _path_is_within(fpath.resolve(), _artifact_dir(token).resolve()):
        return None
    return meta, fpath


def revoke_artifact(token: str) -> bool:
    with _ARTIFACTS_LOCK:
        meta = _load_meta(token)
        if meta is None:
            return False
        meta["revoked_at"] = time.time()
        _write_json_atomic(_meta_path(str(meta.get("token") or token)), meta)
    return True


def list_artifacts() -> list[dict]:
    items: list[dict] = []
    if not ARTIFACTS_DIR.is_dir():
        return items
    for meta_file in sorted(ARTIFACTS_DIR.glob("*/meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(meta, dict) or meta.get("revoked_at"):
            continue
        versions = meta.get("versions") or []
        items.append({
            "token": meta.get("token"),
            "url": f"/artifact/{meta.get('token')}",
            "title": meta.get("title") or meta.get("filename") or "Untitled",
            "filename": meta.get("filename"),
            "mime": meta.get("mime"),
            "public": bool(meta.get("public")),
            "version": int(versions[-1].get("v") or len(versions)) if versions else 0,
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
        })
    items.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    return items
