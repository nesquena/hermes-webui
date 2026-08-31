"""Trusted, read-only discovery for configured Claude Code session stores."""
from __future__ import annotations

import datetime
import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ClaudeModelProfile:
    model_id: str
    label: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class ClaudeStore:
    store_id: str
    label: str
    config_dir: Path
    projects_dir: Path
    claude_bin: Path
    workspace_roots: tuple[Path, ...]
    models: Mapping[str, ClaudeModelProfile]


@dataclass(frozen=True)
class ClaudeSessionDescriptor:
    public_id: str
    store: ClaudeStore
    claude_session_id: str
    transcript_path: Path
    profile: ClaudeModelProfile | None
    cwd: Path | None
    workspace_label: str | None
    messages: tuple[dict, ...]
    title: str
    created_at: float | None
    updated_at: float | None
    file_updated_at: float
    can_remote_resume: bool


CLAUDE_CODE_MAX_FILES = 200
CLAUDE_CODE_MAX_FILE_BYTES = 10 * 1024 * 1024
CLAUDE_CODE_MAX_MESSAGES_PER_FILE = 1000
CLAUDE_CODE_MAX_CONTENT_CHARS = 200_000
CLAUDE_CODE_MAX_LINES_PER_FILE = 100_000
CLAUDE_CODE_MAX_CANDIDATES = 1_000
CLAUDE_CODE_MAX_PROJECT_DIRS = 1_000
CLAUDE_STORES_FILE_MAX_BYTES = 64 * 1024

_CANONICAL_MODEL_PROFILES = {
    "anthropic.qwen-aeon": (
        "qwen",
        "Claude Qwen",
        "/Users/mohameddarwiche/bin/claude-qwen",
    ),
    "anthropic.ornith": (
        "ornith",
        "Claude Local · Ornith",
        "/Users/mohameddarwiche/bin/claude-ornith",
    ),
}


def _safe_existing_path(value, *, directory: bool = False, executable: bool = False) -> Path | None:
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        return None
    path = Path(os.path.abspath(path))
    current = Path(path.anchor)
    try:
        for component in path.parts[1:]:
            current /= component
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return None
        info = os.stat(path)
    except OSError:
        return None
    if info.st_uid != os.getuid():
        return None
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            return None
    elif not stat.S_ISREG(info.st_mode):
        return None
    if executable and not info.st_mode & stat.S_IXUSR:
        return None
    return path


def _contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _load_store(raw) -> ClaudeStore | None:
    if not isinstance(raw, dict):
        return None
    store_id = raw.get("id")
    label = raw.get("label")
    if not isinstance(store_id, str) or not store_id or not isinstance(label, str) or not label:
        return None
    config_dir = _safe_existing_path(raw.get("config_dir"), directory=True)
    claude_bin = _safe_existing_path(raw.get("claude_bin"), executable=True)
    roots_raw = raw.get("workspace_roots")
    models_raw = raw.get("models")
    if config_dir is None or claude_bin is None or not isinstance(roots_raw, list) or not isinstance(models_raw, dict):
        return None
    projects_dir = _safe_existing_path(str(config_dir / "projects"), directory=True)
    if projects_dir is None:
        return None
    roots: list[Path] = []
    for raw_root in roots_raw:
        root = _safe_existing_path(raw_root, directory=True)
        if root is None or root in roots:
            return None
        roots.append(root)
    if not roots:
        return None
    models: dict[str, ClaudeModelProfile] = {}
    for model_id, profile_raw in models_raw.items():
        canonical = _CANONICAL_MODEL_PROFILES.get(model_id)
        if canonical is None or not isinstance(profile_raw, dict):
            return None
        argv_raw = profile_raw.get("argv")
        if not isinstance(argv_raw, list) or not argv_raw or argv_raw[0] != canonical[2]:
            return None
        executable = _safe_existing_path(argv_raw[0], executable=True)
        if executable is None or any(not isinstance(arg, str) or not arg for arg in argv_raw):
            return None
        models[model_id] = ClaudeModelProfile(
            model_id=model_id,
            label=canonical[1],
            argv=(str(executable), *argv_raw[1:]),
        )
    if not models:
        return None
    return ClaudeStore(
        store_id=store_id,
        label=label,
        config_dir=config_dir,
        projects_dir=projects_dir,
        claude_bin=claude_bin,
        workspace_roots=tuple(roots),
        models=MappingProxyType(models),
    )


def _identity(info) -> tuple:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _read_private_registry(registry: Path):
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(registry, os.O_RDONLY | nofollow_flag)
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size > CLAUDE_STORES_FILE_MAX_BYTES
        ):
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(8192, CLAUDE_STORES_FILE_MAX_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > CLAUDE_STORES_FILE_MAX_BYTES:
                return None
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
        after = os.fstat(fd)
        if _identity(before) != _identity(after):
            return None
        return payload
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    finally:
        os.close(fd)


def load_claude_stores() -> tuple[ClaudeStore, ...]:
    """Load only a private, operator-owned registry from the environment."""
    registry = _safe_existing_path(os.getenv("HERMES_WEBUI_CLAUDE_STORES_FILE"))
    if registry is None:
        return ()
    payload = _read_private_registry(registry)
    if payload is None:
        return ()
    stores_raw = payload.get("stores") if isinstance(payload, dict) else None
    if not isinstance(stores_raw, list):
        return ()
    stores: list[ClaudeStore] = []
    for raw in stores_raw:
        store = _load_store(raw)
        if store is None or any(existing.store_id == store.store_id for existing in stores):
            return ()
        if any(
            _contains(existing.config_dir, store.config_dir)
            or _contains(store.config_dir, existing.config_dir)
            for existing in stores
        ):
            return ()
        stores.append(store)
    return tuple(stores)


def invalidate_claude_session_cache() -> None:
    """Compatibility hook for callers that refresh Claude session projections."""
    return None


def _parse_timestamp(value) -> float | None:
    if isinstance(value, (int, float)):
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except ValueError:
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
            return parsed if math.isfinite(parsed) else None
        except ValueError:
            return None


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content[:CLAUDE_CODE_MAX_CONTENT_CHARS]
    if isinstance(content, list):
        parts: list[str] = []
        used = 0
        for item in content:
            text = item if isinstance(item, str) else item.get("text") or item.get("content") if isinstance(item, dict) else ""
            if not text:
                continue
            text = str(text)
            remaining = CLAUDE_CODE_MAX_CONTENT_CHARS - used
            if remaining <= 0:
                break
            parts.append(text[:remaining])
            used += len(parts[-1])
        return "\n".join(parts)
    if isinstance(content, dict):
        return _extract_text(content.get("text") or content.get("content"))
    return ""


def _public_id(store: ClaudeStore, claude_session_id: str) -> str:
    digest = hashlib.sha256(f"{store.store_id}:{claude_session_id}".encode("utf-8")).hexdigest()[:24]
    return f"claude_code_{digest}"


def _is_uuid(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        import uuid

        parsed = str(uuid.UUID(value))
    except (ValueError, AttributeError):
        return None
    return parsed


def _allowed_workspace(store: ClaudeStore, value) -> Path | None:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        return None
    candidate = Path(os.path.realpath(value))
    for root in store.workspace_roots:
        if _contains(root, candidate):
            return candidate
    return None


def _descriptor_from_fd(store: ClaudeStore, path: Path, fd: int, file_info) -> ClaudeSessionDescriptor | None:
    stem_id = _is_uuid(path.stem)
    if stem_id is None or path.suffix.lower() != ".jsonl" or file_info.st_nlink != 1:
        os.close(fd)
        return None
    messages: list[dict] = []
    title: str | None = None
    first_timestamp: float | None = None
    last_timestamp: float | None = None
    transcript_ids: set[str] = set()
    latest_model: str | None = None
    latest_cwd: Path | None = None
    invalid_cwd = False
    line_count = 0
    try:
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(8192, CLAUDE_CODE_MAX_FILE_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > CLAUDE_CODE_MAX_FILE_BYTES:
                return None
            chunks.append(chunk)
        for line in b"".join(chunks).splitlines():
            line_count += 1
            if line_count > CLAUDE_CODE_MAX_LINES_PER_FILE:
                return None
            try:
                raw = json.loads(line.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            session_id = raw.get("sessionId")
            if session_id is not None:
                normalized_id = _is_uuid(session_id)
                if normalized_id is None or normalized_id != stem_id:
                    return None
                transcript_ids.add(normalized_id)
            if raw.get("isSidechain") or raw.get("isSubagent"):
                continue
            cwd_value = raw.get("cwd")
            if cwd_value is not None:
                latest_cwd = _allowed_workspace(store, cwd_value)
                invalid_cwd = latest_cwd is None
            record = raw.get("message") if isinstance(raw.get("message"), dict) else raw
            role = str(record.get("role") or raw.get("role") or raw.get("type") or "").lower()
            if role == "human":
                role = "user"
            if role == "assistant" and "model" in record:
                model = record.get("model")
                if model != "<synthetic>":
                    latest_model = model if isinstance(model, str) else None
            if not title:
                summary = raw.get("summary") or raw.get("title")
                if isinstance(summary, str) and summary.strip():
                    title = " ".join(summary.split())[:80]
            if role not in {"user", "assistant", "system", "tool"} or len(messages) >= CLAUDE_CODE_MAX_MESSAGES_PER_FILE:
                continue
            content = _extract_text(record.get("content") if "content" in record else raw.get("content"))
            if not content.strip():
                continue
            timestamp = _parse_timestamp(
                record.get("timestamp") or raw.get("timestamp") or raw.get("created_at")
            )
            if timestamp is not None:
                first_timestamp = timestamp if first_timestamp is None else min(first_timestamp, timestamp)
                last_timestamp = timestamp if last_timestamp is None else max(last_timestamp, timestamp)
            messages.append({"role": role, "content": content, **({"timestamp": timestamp} if timestamp is not None else {})})
        if _identity(file_info) != _identity(os.fstat(fd)):
            return None
    except (OSError, UnicodeError):
        return None
    finally:
        os.close(fd)
    if transcript_ids != {stem_id}:
        return None
    profile = store.models.get(latest_model) if latest_model else None
    can_remote_resume = profile is not None and latest_cwd is not None and not invalid_cwd
    if not title:
        title = next((" ".join(str(message["content"]).split())[:80] for message in messages if message["role"] == "user"), "Claude Code Session")
    return ClaudeSessionDescriptor(
        public_id=_public_id(store, stem_id),
        store=store,
        claude_session_id=stem_id,
        transcript_path=path,
        profile=profile,
        cwd=latest_cwd,
        workspace_label=latest_cwd.name if latest_cwd else None,
        messages=tuple(messages),
        title=title,
        created_at=first_timestamp,
        updated_at=last_timestamp,
        file_updated_at=file_info.st_mtime,
        can_remote_resume=can_remote_resume,
    )


def _store_descriptors(store: ClaudeStore) -> list[ClaudeSessionDescriptor]:
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[ClaudeSessionDescriptor] = []
    root_fd = None
    project_count = 0
    candidate_count = 0
    try:
        root_fd = os.open(store.projects_dir, os.O_RDONLY | directory_flag | nofollow_flag)
        with os.scandir(root_fd) as projects:
            for project in projects:
                project_count += 1
                if project_count > CLAUDE_CODE_MAX_PROJECT_DIRS:
                    return []
                if not project.is_dir(follow_symlinks=False):
                    continue
                try:
                    project_fd = os.open(project.name, os.O_RDONLY | directory_flag | nofollow_flag, dir_fd=root_fd)
                except OSError:
                    continue
                try:
                    with os.scandir(project_fd) as files:
                        for entry in files:
                            candidate_count += 1
                            if candidate_count > CLAUDE_CODE_MAX_CANDIDATES:
                                return []
                            if not entry.name.endswith(".jsonl") or not entry.is_file(follow_symlinks=False):
                                continue
                            file_fd = None
                            try:
                                before = entry.stat(follow_symlinks=False)
                                if before.st_uid != os.getuid() or before.st_size > CLAUDE_CODE_MAX_FILE_BYTES or before.st_nlink != 1:
                                    continue
                                file_fd = os.open(entry.name, os.O_RDONLY | nofollow_flag, dir_fd=project_fd)
                                after = os.fstat(file_fd)
                                if _identity(before) != _identity(after) or after.st_uid != os.getuid():
                                    continue
                                path = store.projects_dir / project.name / entry.name
                                descriptor = _descriptor_from_fd(store, path, file_fd, after)
                                file_fd = None
                            except OSError:
                                continue
                            finally:
                                if file_fd is not None:
                                    os.close(file_fd)
                            if descriptor is not None:
                                descriptors.append(descriptor)
                finally:
                    os.close(project_fd)
    except OSError:
        return []
    finally:
        try:
            os.close(root_fd)
        except (OSError, TypeError):
            pass
    by_uuid: dict[str, list[ClaudeSessionDescriptor]] = {}
    for descriptor in descriptors:
        by_uuid.setdefault(descriptor.claude_session_id, []).append(descriptor)
    return [
        descriptor
        for descriptor in descriptors
        if len(by_uuid[descriptor.claude_session_id]) == 1
    ][:CLAUDE_CODE_MAX_FILES]


def _descriptors() -> list[ClaudeSessionDescriptor]:
    descriptors: list[ClaudeSessionDescriptor] = []
    for store in load_claude_stores():
        # UUID uniqueness is store-local. Opaque public IDs include the store
        # ID, keeping equal UUIDs from distinct configured stores unambiguous.
        descriptors.extend(_store_descriptors(store))
    return descriptors


def _profile_key(profile: ClaudeModelProfile | None) -> str | None:
    if profile is None:
        return None
    canonical = _CANONICAL_MODEL_PROFILES.get(profile.model_id)
    return canonical[0] if canonical is not None else None


def _public_projection(descriptor: ClaudeSessionDescriptor) -> dict:
    profile_key = _profile_key(descriptor.profile)
    label = descriptor.profile.label if descriptor.profile else descriptor.store.label
    timestamp = descriptor.updated_at or descriptor.file_updated_at
    return {
        "session_id": descriptor.public_id,
        "title": descriptor.title,
        "workspace": descriptor.workspace_label or "Claude Code",
        "model": "claude-code",
        "message_count": len(descriptor.messages),
        "created_at": descriptor.created_at or timestamp,
        "updated_at": timestamp,
        "last_message_at": timestamp,
        "pinned": False,
        "archived": False,
        "project_id": None,
        "profile": profile_key,
        "source_tag": "claude_code",
        "raw_source": "claude_code",
        "session_source": "external_agent",
        "source_label": "Claude Code",
        "is_cli_session": True,
        "read_only": True,
        "kind": "claude_code",
        "label": label,
        "can_remote_resume": descriptor.can_remote_resume,
        "coarse_status": "inactive",
        "workspace_label": descriptor.workspace_label or "Claude Code",
    }


def list_public_sessions() -> list[dict]:
    """Return browser-safe, read-only Claude Code session rows."""
    rows = [_public_projection(descriptor) for descriptor in _descriptors()]
    rows.sort(key=lambda row: row["last_message_at"] or 0, reverse=True)
    return rows


def resolve_session(public_id) -> ClaudeSessionDescriptor | None:
    """Re-resolve an opaque public row ID from configured stores."""
    for descriptor in _descriptors():
        if descriptor.public_id == str(public_id or ""):
            return descriptor
    return None
