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

Security model
--------------
*Ownership is a stable principal, not a cookie.* An artifact records the
authenticated principal (auth type + username, or ``local`` for a
single-secret deployment) and the profile it was published under. A re-login,
a session expiry or a profile switch therefore no longer orphans or re-homes
somebody's durable artifacts.

*The browser never supplies path authority.* A publish must present a
server-minted source capability: an HMAC over the resolved path bound to the
principal, the profile, the session and an expiry. The mint step
(``prepare_source_capability``) is the only place a path is validated, and it
validates against roots derived from the REQUESTING profile — not from process
globals — with WebUI/Hermes state denied by absolute canonical subtree. A
workspace carve-out can no longer re-admit state.

*Public means redacted.* ``public_safe`` is set only when the stored copy
actually went through the credential redactor. An unknown or binary format
cannot be published publicly unless the caller passes an explicit
``verbatim_public`` acknowledgement, and even then it is recorded as verbatim.

*Revocable, therefore never positively cacheable.* Every artifact response is
``no-store`` and revoking deletes the stored bytes. The previous
``immutable``/one-year contract meant a revoked public URL could keep being
served by an intermediary for a year.

The feature as a whole is opt-in: settings["artifacts_enabled"], default off,
env override HERMES_WEBUI_ARTIFACTS (generic deployment; nothing TARS-specific).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import contextlib
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
_ARTIFACTS_LOCK = threading.RLock()
_ARTIFACTS_LOCK_DEPTH = 0

try:  # POSIX only; Windows keeps the thread-level lock alone.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - platform dependent
    _fcntl = None


@contextlib.contextmanager
def _artifacts_lock():
    """Serialise artifact mutations across THREADS and PROCESSES.

    A ``threading.Lock`` only orders callers inside one interpreter, but the
    artifact store is a directory that more than one process writes: the WebUI
    and the gateway can run separately over the same Hermes home, and an
    operator can run a maintenance command alongside either. Version numbers
    are allocated read-max-plus-one, so two processes holding only their own
    thread lock hand out the SAME number, and the second publication overwrites
    the first — silently, because both wrote a well-formed record.

    An advisory ``flock`` on a lock file in the artifact directory closes that.
    It is advisory, which is enough here: every writer goes through this module.
    The depth counter keeps a nested acquisition from taking a second flock on a
    new descriptor, which would deadlock against this process's own lock.
    """
    global _ARTIFACTS_LOCK_DEPTH
    with _ARTIFACTS_LOCK:
        if _fcntl is None or _ARTIFACTS_LOCK_DEPTH > 0:
            _ARTIFACTS_LOCK_DEPTH += 1
            try:
                yield
            finally:
                _ARTIFACTS_LOCK_DEPTH -= 1
            return
        try:
            ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(ARTIFACTS_DIR / ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            # An unwritable store is a problem the caller will hit anyway, with
            # a better message than a lock failure.
            _ARTIFACTS_LOCK_DEPTH += 1
            try:
                yield
            finally:
                _ARTIFACTS_LOCK_DEPTH -= 1
            return
        _ARTIFACTS_LOCK_DEPTH += 1
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
            yield
        finally:
            _ARTIFACTS_LOCK_DEPTH -= 1
            try:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _fsync_dir(path: Path) -> None:
    """Flush a directory entry so a rename survives a crash.

    Without this the rename can still be in the page cache when the metadata
    commit lands, which is the ordering the staged publication exists to
    guarantee.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)

# Hard cap per published file. Artifacts are chat deliverables (reports,
# charts, small bundles), not a file-hosting service.
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024

# ── Bounds (finding 6) ───────────────────────────────────────────────────────
# Without these a long-lived install accumulates artifacts, versions and bytes
# forever, every list/lookup is an O(N) directory scan, and there is no GC for
# revoked or orphaned storage.
MAX_ARTIFACTS_PER_OWNER = 200
MAX_VERSIONS_PER_ARTIFACT = 20
MAX_TOTAL_BYTES_PER_OWNER = 500 * 1024 * 1024
LIST_PAGE_SIZE_DEFAULT = 100
LIST_PAGE_SIZE_MAX = 500

# How long a minted source capability stays valid. Long enough for a user to
# click Publish, short enough that a leaked capability is not a durable grant.
SOURCE_CAPABILITY_TTL_SECONDS = 600

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

# Text-like MIME types the credential redactor understands. ONLY these can be
# published publicly through the redacted path; everything else needs the
# explicit verbatim acknowledgement (finding 3: this used to fail open — a
# `.log`, `.yaml`, `.py`, `.env.production`, PDF or metadata-bearing image was
# copied byte-for-byte and then marked public_safe).
_REDACTABLE_MIME = {"text/html", "text/plain", "application/json", "image/svg+xml"}

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

# State directory names denied under a profile home AND under its
# ``webui_state`` subtree. Mirrors the /api/media deny set (#3234) so the two
# publishable-surface gates cannot drift apart.
_DENIED_STATE_DIRNAMES = (
    "sessions", "memories", "cron", "logs", "checkpoints", "backups",
    "webui", "profiles", "auth", "state", "kanban", "telemetry",
    "shares", "artifacts", "_drafts",
)


class ArtifactCapabilityError(ValueError):
    """The publish request carried no valid server-minted source capability."""


class ArtifactPublicUnsafe(ValueError):
    """Public publication refused because the content cannot be redacted."""


class ArtifactQuotaExceeded(ValueError):
    """A per-owner artifact/version/byte bound would be exceeded."""


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


# ── Owner identity (finding 2) ───────────────────────────────────────────────


def owner_key(principal: str | None, profile: str | None) -> str | None:
    """The durable ownership key: a stable principal within one profile.

    ``None`` is the no-auth deployment mode, where artifacts keep their
    historical shared behavior.
    """
    if principal is None:
        return None
    return f"{principal}@{str(profile or 'default')}"


def _meta_owner_key(meta: dict) -> str | None:
    principal = meta.get("principal")
    if not principal:
        return None  # legacy meta: owned by a session token, see _meta_owned_by
    return owner_key(str(principal), meta.get("profile"))


def _meta_is_legacy(meta: dict) -> bool:
    """True for artifacts published before ownership became a stable principal.

    Their recorded ``owner`` is a random session token that can never match a
    live request, so there is no way to tell from the record WHICH user
    published them.
    """
    return not meta.get("principal")


def _meta_owned_by(meta: dict, owner: str | None) -> bool:
    """THE ownership predicate. List, GET, re-publish and revoke all use this.

    A legacy record fails CLOSED. Treating "we cannot tell who owns this" as
    "everyone owns this" let any authenticated principal read a stranger's
    artifact and then adopt it — first-come takeover, in a deployment where
    separate principals are the whole point of having owners. The bytes are
    still on disk for an operator to migrate deliberately; what is gone is the
    silent claim.

    ``owner is None`` remains the no-auth deployment, where artifacts keep
    their historical shared behaviour because there is no principal to scope
    them to.
    """
    if owner is None:
        return True  # no-auth deployment
    if _meta_is_legacy(meta):
        return False
    return _meta_owner_key(meta) == owner


def _adopt_legacy_owner(meta: dict, owner: str | None) -> None:
    """Stamp an owning principal onto a record that has none.

    Only reachable once ``_meta_owned_by()`` has already admitted the caller,
    which for a legacy record it no longer does — so this now runs solely for
    the no-auth-to-auth transition, where every field is written TOGETHER.
    Updating ``principal``/``profile`` while leaving the stale token in
    ``owner`` used to leave the record in a state no predicate accepted: the
    adopter had taken it, and could then no longer read it.
    """
    if owner is None or not _meta_is_legacy(meta):
        return
    principal, _, profile = str(owner).rpartition("@")
    meta["principal"] = principal or str(owner)
    meta["profile"] = profile or "default"
    meta["owner"] = owner
    meta["migrated_at"] = time.time()


# ── Source capability (finding 1) ────────────────────────────────────────────


def _capability_key() -> bytes:
    from api.auth import _signing_key

    return _signing_key()


def source_fingerprint(st) -> str:
    """Identity of the exact bytes a validation step looked at.

    Device+inode alone would still admit a file whose CONTENT was replaced in
    place between prepare and publish, so size and mtime travel with them.
    """
    return f"{st.st_dev}:{st.st_ino}:{st.st_size}:{st.st_mtime_ns}"


def _capability_payload(
    source: str,
    owner: str | None,
    session_id: str | None,
    fingerprint: str,
    expires: int,
) -> str:
    # v2: the fingerprint is part of the signed payload. Under v1 the capability
    # authorized a PATHNAME, so anything that path came to mean before the
    # publish call — a different file swapped in under the same name — was
    # published under a signature issued for something else.
    return "\x1f".join([
        "artifact-source-v2",
        str(source),
        str(owner or "-"),
        str(session_id or "-"),
        str(fingerprint),
        str(int(expires)),
    ])


def mint_source_capability(
    source: str, *, owner: str | None, session_id: str | None = None,
    fingerprint: str,
) -> dict:
    """HMAC capability binding validated BYTES to who may publish them."""
    expires = int(time.time()) + SOURCE_CAPABILITY_TTL_SECONDS
    payload = _capability_payload(source, owner, session_id, fingerprint, expires)
    sig = hmac.new(_capability_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return {"exp": expires, "sig": sig}


def verify_source_capability(
    capability, source: str, *, owner: str | None, session_id: str | None = None,
    fingerprint: str,
) -> None:
    """Raise ArtifactCapabilityError unless *capability* authorizes *source*.

    This is what removes path authority from the browser: a client can no
    longer name an arbitrary readable file, because only the mint step — which
    validates against the requesting profile's roots — produces a signature,
    and the signature is bound to that principal, profile and session.
    """
    if not isinstance(capability, dict):
        raise ArtifactCapabilityError("a source capability is required")
    try:
        expires = int(capability.get("exp") or 0)
    except (TypeError, ValueError):
        raise ArtifactCapabilityError("malformed source capability") from None
    sig = str(capability.get("sig") or "")
    if not sig or expires <= 0:
        raise ArtifactCapabilityError("malformed source capability")
    if expires < time.time():
        raise ArtifactCapabilityError("source capability expired; try publishing again")
    expected = hmac.new(
        _capability_key(),
        _capability_payload(source, owner, session_id, fingerprint, expires).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        # Also the failure mode when the file changed between prepare and
        # publish: the signature covers the fingerprint, so different bytes
        # under the same name no longer verify.
        raise ArtifactCapabilityError(
            "source capability does not authorize this file; if the file "
            "changed, prepare it again"
        )


# ── Source validation (finding 1) ────────────────────────────────────────────


def _request_hermes_home() -> Path | None:
    """The ACTIVE REQUEST profile's Hermes home, not a process global.

    The previous root set was `HERMES_HOME` (process env) plus a hardcoded
    `~/.hermes`, so a named profile could publish out of the root profile's
    tree and vice versa.
    """
    try:
        from api.profiles import get_active_hermes_home

        return Path(get_active_hermes_home()).expanduser().resolve()
    except Exception:
        return None


def _denied_state_roots() -> list[Path]:
    """Absolute canonical subtrees that are never publishable.

    Checked as whole subtrees rather than by first-path-component name. The
    component check missed every real layout: with the default
    `STATE_DIR=<HERMES_HOME>/webui`, `webui/sessions/...` and
    `webui/artifacts/...` are two components deep, and a named profile's state
    starts with `profiles/<name>/...`.
    """
    roots: list[Path] = []

    def _add(path) -> None:
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            return
        if resolved not in roots:
            roots.append(resolved)

    # The WebUI's own state root: sessions, artifacts, settings, drafts.
    _add(STATE_DIR)
    home = _request_hermes_home()
    if home is not None:
        for name in _DENIED_STATE_DIRNAMES:
            _add(home / name)
        # A NAMED profile's WebUI state does not live at <home>/webui — it lives
        # at <home>/webui_state (api/workspace.py: `webui_state/workspaces.json`,
        # `webui_state/last_workspace.txt`, `webui_state/sessions/...`). Denying
        # only "webui" left every named profile's actual chat sessions
        # publishable, because <home> is itself an allowed root. `/api/media`'s
        # deny logic already knew about this directory (Codex review #3234);
        # this list has to know it too.
        for name in _DENIED_STATE_DIRNAMES:
            _add(home / "webui_state" / name)
        _add(home / "webui_state")
    return roots


def _allowed_source_roots() -> list[Path]:
    """Roots a publishable file may live in, derived from THIS request."""
    roots: list[Path] = []

    def _add(path) -> None:
        try:
            resolved = Path(path).expanduser().resolve()
        except Exception:
            return
        if resolved not in roots:
            roots.append(resolved)

    _add("/tmp")
    home = _request_hermes_home()
    if home is not None:
        _add(home)
    try:
        # The ACTIVE PROFILE's workspace, never the process-global one.
        # get_last_workspace() falls back to a global last-workspace file when
        # a named profile has not written its own yet, so a cold profile
        # inherited whatever workspace another profile last used — and that
        # directory then became a publishable source root for this request.
        from api.workspace import get_profile_default_workspace

        ws = Path(get_profile_default_workspace()).expanduser().resolve()
        if ws.is_dir():
            _add(ws)
    except Exception:
        pass
    extra = os.environ.get("ARTIFACT_ALLOWED_ROOTS", "").strip()
    if extra:
        for root in extra.split(os.pathsep):
            root = root.strip()
            if root:
                candidate = Path(root).expanduser()
                try:
                    if candidate.is_dir():
                        _add(candidate)
                except Exception:
                    pass
    return roots


def _path_is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False


def _in_denied_state_subdir(target: Path) -> bool:
    """True when *target* sits inside a denied state subtree.

    There is deliberately NO workspace carve-out. Previously an active
    workspace short-circuited the whole check, so pointing the workspace at (or
    above) a Hermes root re-admitted every state directory it was meant to
    protect.
    """
    return any(_path_is_within(target, root) for root in _denied_state_roots())


def validate_source_path(raw_path: str) -> Path:
    """Resolve and validate a publish source path; raises ValueError with a
    user-facing message on every rejection."""
    raw_path = str(raw_path or "").strip()
    if not raw_path:
        raise ValueError("path is required")
    raw_path = os.path.expanduser(raw_path)
    try:
        target = Path(raw_path).resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError("file not found") from exc
    except Exception as exc:
        raise ValueError("invalid path") from exc
    if not target.is_file():
        raise ValueError("path is not a regular file")
    if target.name.casefold() in {n.casefold() for n in _DENY_FILENAMES}:
        raise ValueError("this file type is not publishable")
    # State denial runs BEFORE the allowlist so a root that overlaps state can
    # never admit it.
    if _in_denied_state_subdir(target):
        raise ValueError("Hermes state directories are not publishable")
    if not any(_path_is_within(target, r) for r in _allowed_source_roots()):
        raise ValueError("path is outside the publishable roots")
    size = target.stat().st_size
    if size == 0:
        raise ValueError("file is empty")
    if size > MAX_ARTIFACT_BYTES:
        raise ValueError("file exceeds the 50 MB artifact limit")
    return target


def prepare_source_capability(
    raw_path: str, *, owner: str | None, session_id: str | None = None
) -> dict:
    """Validate a path for THIS caller and mint the capability to publish it."""
    source = validate_source_path(raw_path)
    # One stat, used for both the fingerprint and the reported size, so the
    # capability describes the bytes that were actually validated.
    st = source.stat()
    capability = mint_source_capability(
        str(source), owner=owner, session_id=session_id,
        fingerprint=source_fingerprint(st),
    )
    return {
        "path": str(source),
        "filename": source.name,
        "mime": mime_for(source.name),
        "size": st.st_size,
        "capability": capability,
    }


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


def _iter_metas():
    if not ARTIFACTS_DIR.is_dir():
        return
    for meta_file in sorted(ARTIFACTS_DIR.glob("*/meta.json")):
        try:
            meta = json.loads(meta_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(meta, dict):
            yield meta


def _find_token_for_source(source: str, *, owner: str | None = None) -> str | None:
    """Existing owner-scoped non-revoked artifact for this resolved source path."""
    for meta in _iter_metas():
        if meta.get("revoked_at"):
            continue
        if meta.get("source_path") == source and _meta_owned_by(meta, owner):
            return str(meta.get("token") or "") or None
    return None


# ── Quotas + GC (finding 6) ──────────────────────────────────────────────────


def _retained_bytes(meta: dict) -> int:
    """Bytes this artifact will still occupy after version pruning."""
    versions = list(meta.get("versions") or [])
    retained = versions[-MAX_VERSIONS_PER_ARTIFACT:] if versions else []
    total = 0
    for ventry in retained:
        try:
            total += int(ventry.get("size") or 0)
        except (TypeError, ValueError):
            pass
    return total


def _owner_usage(owner: str | None, *, exclude_token: str | None = None) -> tuple[int, int]:
    """(artifact_count, total_bytes) currently attributed to *owner*.

    ``exclude_token`` omits an artifact from the COUNT only; its bytes are
    always included. Excluding a re-published artifact wholesale let the byte
    cap be walked past indefinitely: each same-token publish compared only the
    ONE incoming version against everything else, while its own 1..N retained
    versions were never counted — up to MAX_VERSIONS_PER_ARTIFACT ×
    MAX_ARTIFACT_BYTES per token, well over the per-owner cap.
    """
    count = 0
    total = 0
    for meta in _iter_metas():
        if meta.get("revoked_at"):
            continue
        if not _meta_owned_by(meta, owner):
            continue
        total += _retained_bytes(meta)
        if exclude_token and str(meta.get("token") or "") == exclude_token:
            continue
        count += 1
    return count, total


def _enforce_quotas(owner: str | None, token: str | None, incoming_bytes: int) -> None:
    count, total = _owner_usage(owner, exclude_token=token)
    if token is None and count + 1 > MAX_ARTIFACTS_PER_OWNER:
        raise ArtifactQuotaExceeded(
            f"artifact limit reached ({MAX_ARTIFACTS_PER_OWNER}); revoke one first"
        )
    if total + incoming_bytes > MAX_TOTAL_BYTES_PER_OWNER:
        raise ArtifactQuotaExceeded(
            "artifact storage limit reached; revoke an artifact to free space"
        )


def _prune_versions(token: str, meta: dict) -> None:
    """Keep only the newest MAX_VERSIONS_PER_ARTIFACT versions on disk."""
    versions = list(meta.get("versions") or [])
    if len(versions) <= MAX_VERSIONS_PER_ARTIFACT:
        return
    drop = versions[: len(versions) - MAX_VERSIONS_PER_ARTIFACT]
    for ventry in drop:
        try:
            vdir = _artifact_dir(token) / f"v{int(ventry.get('v') or 0)}"
        except ValueError:
            continue
        shutil.rmtree(vdir, ignore_errors=True)
    meta["versions"] = versions[len(versions) - MAX_VERSIONS_PER_ARTIFACT:]


def _delete_artifact_storage(token: str) -> None:
    """Remove every stored byte for *token*, keeping only the tombstone meta."""
    try:
        adir = _artifact_dir(token)
    except ValueError:
        return
    if not adir.is_dir():
        return
    for child in adir.iterdir():
        if child.name == "meta.json":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            try:
                child.unlink()
            except OSError:
                pass


def gc_artifacts() -> int:
    """Drop storage for revoked artifacts and directories with no meta."""
    removed = 0
    if not ARTIFACTS_DIR.is_dir():
        return removed
    with _artifacts_lock():
        for child in ARTIFACTS_DIR.iterdir():
            if not child.is_dir():
                continue
            meta_file = child / "meta.json"
            if not meta_file.is_file():
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
                continue
            try:
                meta = json.loads(meta_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(meta, dict) and meta.get("revoked_at"):
                before = any(p.name != "meta.json" for p in child.iterdir())
                _delete_artifact_storage(child.name)
                if before:
                    removed += 1
    return removed


# ── Race-free copy (finding 5) ───────────────────────────────────────────────


def _open_source_checked(source: Path, expected_stat) -> int:
    """Open *source* under an allowed root and verify it is the validated file.

    Validation resolved and stat'ed a pathname; the copy below must act on the
    SAME file, not on whatever that name points at a moment later.

    ``O_NOFOLLOW`` on the full pathname was not enough: it protects only the
    LAST component, while the kernel still resolves every parent by name. So a
    parent swapped for a symlink after the containment check redirected the
    read to a file that was never validated — containment had been proven
    against a resolved string, and a string does not stay true.

    The open therefore walks down from the allowed root that admitted this
    path, refusing a symlink at every component, and the resulting descriptor
    is fstat-compared against the validated identity. The walk is
    ``api.workspace.open_anchored_fd`` — the same primitive the serving path
    uses, rather than a second implementation of it here.
    """
    import stat as _stat

    from api.workspace import open_anchored_fd

    roots = [r for r in _allowed_source_roots() if _path_is_within(source, r)]
    if not roots:
        raise ValueError("path is outside the publishable roots")
    # Deepest matching root: the fewest components to walk, and the tightest
    # anchor if roots are nested.
    root = max(roots, key=lambda r: len(r.parts))

    try:
        fd = open_anchored_fd(root, source, want_dir=False)
    except (FileNotFoundError, ValueError) as exc:
        # A missing component, a wrong type, or a component swapped to a
        # symlink. All of them mean the same thing here: this is not the file
        # that was validated.
        raise ValueError("source file changed while publishing") from exc
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise ValueError("path is not a regular file")
        if (st.st_dev, st.st_ino) != (expected_stat.st_dev, expected_stat.st_ino):
            raise ValueError("source file changed while publishing")
        if st.st_size > MAX_ARTIFACT_BYTES:
            raise ValueError("file exceeds the 50 MB artifact limit")
        return fd
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _read_source_bytes(source: Path, expected_stat) -> bytes:
    fd = _open_source_checked(source, expected_stat)
    try:
        with os.fdopen(fd, "rb", closefd=True) as fh:
            return fh.read(MAX_ARTIFACT_BYTES + 1)
    except Exception:
        raise


def publish_artifact(
    raw_path: str,
    *,
    title: str | None = None,
    public: bool | None = None,
    session_id: str | None = None,
    token: str | None = None,
    owner: str | None = None,
    capability=None,
    verbatim_public: bool = False,
) -> dict:
    """Publish (or re-publish) a file as a new artifact version.

    Requires a server-minted source ``capability`` for *raw_path* bound to this
    ``owner``/``session_id`` (see ``prepare_source_capability``).

    ``public`` tri-state: True/False set the flag, None PRESERVES the current
    value (so a plain re-publish from the UI never silently un-shares a
    public artifact).
    """
    source = validate_source_path(raw_path)
    # One stat feeds the capability check, the quota and the identity the read
    # descriptor is fstat-compared against, so all three describe one file.
    source_stat = source.stat()
    verify_source_capability(
        capability, str(source), owner=owner, session_id=session_id,
        fingerprint=source_fingerprint(source_stat),
    )
    mime = mime_for(source.name)

    with _artifacts_lock():
        ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
        if token:
            token = str(token).strip()
            meta = _load_meta(token)
            if meta is None or meta.get("revoked_at"):
                raise ValueError("unknown or revoked artifact token")
            if not _meta_owned_by(meta, owner):
                raise PermissionError("artifact is not owned by this request")
            _adopt_legacy_owner(meta, owner)
        else:
            token = _find_token_for_source(str(source), owner=owner)
            meta = _load_meta(token) if token else None
            if meta is not None:
                _adopt_legacy_owner(meta, owner)
        if meta is None:
            token = secrets.token_urlsafe(18)
            principal, _, profile = str(owner or "").rpartition("@")
            meta = {
                "token": token,
                "source_path": str(source),
                "filename": source.name,
                "mime": mime,
                "title": "",
                "public": False,
                # Stable identity (finding 2). `owner` is kept for readability
                # in the on-disk meta; authority is principal+profile.
                "principal": principal or None,
                "profile": profile or None,
                "owner": str(owner or ""),
                "session_id": str(session_id or ""),
                "created_at": time.time(),
                "updated_at": None,
                "revoked_at": None,
                "versions": [],
            }

        token = str(token)
        effective_public = bool(meta.get("public")) if public is None else bool(public)

        # Public safety fails CLOSED (finding 3): only content the redactor
        # actually understands may be published publicly. Anything else needs
        # an explicit verbatim acknowledgement and is recorded as such.
        redactable = mime in _REDACTABLE_MIME
        if effective_public and not redactable and not verbatim_public:
            raise ArtifactPublicUnsafe(
                f"{mime or 'this format'} cannot be redacted, so it cannot be made "
                "public automatically. Re-publish with an explicit verbatim "
                "acknowledgement if you intend to expose the file as-is."
            )

        _enforce_quotas(owner, token if meta.get("versions") else None, source_stat.st_size)

        # Monotonic from the highest version ever recorded, NOT from the list
        # length: pruning old versions would otherwise restart the numbering
        # and a new version would collide with a pinned ?v=N link's directory.
        existing = meta.get("versions") or []
        highest = 0
        for entry in existing:
            try:
                highest = max(highest, int(entry.get("v") or 0))
            except (TypeError, ValueError):
                continue
        version = max(highest, int(meta.get("last_version") or 0)) + 1
        # Stage the version, fsync it, then move it into place as one step.
        #
        # Writing straight to v<N>/ published a half-written directory: a crash
        # or a full disk between the write and the metadata commit left bytes
        # that no record pointed at, and a reader that arrived mid-write saw a
        # truncated file under a name the metadata was about to bless. The
        # rename is atomic within the artifact directory, so a version is
        # either entirely there or not there at all.
        art_dir = _artifact_dir(token)
        art_dir.mkdir(parents=True, exist_ok=True)
        vdir = art_dir / f"v{version}"
        staging = art_dir / f".staging-v{version}-{os.getpid()}-{secrets.token_hex(4)}"
        staging.mkdir(parents=True, exist_ok=False)
        dest = staging / source.name

        redacted = False
        verbatim = False
        try:
            try:
                raw = _read_source_bytes(source, source_stat)
            except ValueError:
                raise
            except Exception as exc:
                raise ValueError(f"could not read file: {exc}") from exc
            if len(raw) > MAX_ARTIFACT_BYTES:
                raise ValueError("file exceeds the 50 MB artifact limit")
            if effective_public and redactable:
                text = raw.decode("utf-8", errors="replace")
                payload = _force_redact_credentials(text).encode("utf-8")
                redacted = True
            else:
                payload = raw
                verbatim = bool(effective_public and not redactable)

            fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            stored_size = dest.stat().st_size
            _fsync_dir(staging)
            os.rename(str(staging), str(vdir))
            _fsync_dir(art_dir)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise

        now = time.time()
        meta["versions"] = list(meta.get("versions") or []) + [{
            "v": version,
            "size": stored_size,
            "created_at": now,
            # Per-version serving identity: resolve_artifact_file must locate
            # THIS version's file even if a later re-publish uses a different
            # source filename/MIME (pinned ?v=N links stay valid).
            "filename": source.name,
            "mime": mime,
            # public_safe: this stored copy may be served anonymously. Only a
            # copy that was actually redacted, or one the operator explicitly
            # acknowledged as verbatim, qualifies.
            "public_safe": bool(effective_public and (redacted or verbatim)),
            "redacted": redacted,
            "verbatim_public": verbatim,
        }]
        _prune_versions(token, meta)
        # Survives pruning: the pruned list no longer carries the high-water
        # mark, and version numbers must never be reused.
        meta["last_version"] = version
        meta["updated_at"] = now
        meta["mime"] = mime
        meta["filename"] = source.name
        meta["source_path"] = str(source)
        if title is not None and str(title).strip():
            meta["title"] = str(title).strip()[:200]
        elif not meta.get("title"):
            meta["title"] = source.name
        meta["public"] = effective_public
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


def resolve_artifact_file(token: str, version: int | None = None) -> tuple[dict, dict, Path] | None:
    """(meta, version_entry, file_path) for a live artifact version, or None."""
    meta = _load_meta(token)
    if meta is None or meta.get("revoked_at"):
        return None
    versions = meta.get("versions") or []
    if not versions:
        return None
    if version is None:
        ventry = versions[-1]
    else:
        version = int(version)
        ventry = next((v for v in versions if int(v.get("v") or 0) == version), None)
        if ventry is None:
            return None
    vnum = int(ventry.get("v") or 0)
    try:
        vdir = _artifact_dir(str(meta.get("token") or token)) / f"v{vnum}"
    except ValueError:
        return None
    # Per-version filename (pre-fix metas fall back to the artifact-level name).
    fname = str(ventry.get("filename") or meta.get("filename") or "")
    fpath = (vdir / fname) if fname else None
    if not fpath or not fpath.is_file():
        return None
    # Belt-and-braces: the served file must stay inside this artifact's dir.
    if not _path_is_within(fpath.resolve(), _artifact_dir(token).resolve()):
        return None
    return meta, ventry, fpath


def revoke_artifact(token: str, *, owner: str | None = None) -> bool:
    """Revoke an artifact AND delete its stored bytes.

    Deletion is part of revocation (finding 4): responses are `no-store`, but a
    tombstone that leaves the bytes on disk still keeps the content one
    metadata edit away from being served again.
    """
    with _artifacts_lock():
        meta = _load_meta(token)
        if meta is None or not _meta_owned_by(meta, owner):
            return False
        _adopt_legacy_owner(meta, owner)
        meta["revoked_at"] = time.time()
        real_token = str(meta.get("token") or token)
        _write_json_atomic(_meta_path(real_token), meta)
        _delete_artifact_storage(real_token)
    return True


def list_artifacts(
    *, owner: str | None = None, offset: int = 0, limit: int | None = None
) -> list[dict]:
    """Owner-scoped artifact list. Bounded: see ``list_artifacts_page``."""
    return list_artifacts_page(owner=owner, offset=offset, limit=limit)["artifacts"]


def list_artifacts_page(
    *, owner: str | None = None, offset: int = 0, limit: int | None = None
) -> dict:
    """Paginated owner-scoped artifact list."""
    try:
        offset = max(0, int(offset or 0))
    except (TypeError, ValueError):
        offset = 0
    if limit is None:
        limit = LIST_PAGE_SIZE_DEFAULT
    try:
        limit = max(1, min(LIST_PAGE_SIZE_MAX, int(limit)))
    except (TypeError, ValueError):
        limit = LIST_PAGE_SIZE_DEFAULT

    rows: list[dict] = []
    for meta in _iter_metas():
        if meta.get("revoked_at") or not _meta_owned_by(meta, owner):
            continue
        versions = meta.get("versions") or []
        rows.append({
            "token": meta.get("token"),
            "url": f"/artifact/{meta.get('token')}",
            "title": meta.get("title") or meta.get("filename") or "Untitled",
            "filename": meta.get("filename"),
            "mime": meta.get("mime"),
            "public": bool(meta.get("public")),
            "version": int(versions[-1].get("v") or len(versions)) if versions else 0,
            "created_at": meta.get("created_at"),
            "updated_at": meta.get("updated_at"),
            # Surfaced so the UI can explain the one-time migration window.
            "legacy_unclaimed": _meta_is_legacy(meta),
        })
    rows.sort(key=lambda x: x.get("updated_at") or 0, reverse=True)
    total = len(rows)
    page = rows[offset:offset + limit]
    return {
        "artifacts": page,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < total,
    }
