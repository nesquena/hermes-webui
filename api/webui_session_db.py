"""Dormant JSON-backed SessionDB-shaped adapter for WebUI sessions.

This module intentionally does not replace existing WebUI runtime call sites.
It provides a small compatibility surface over the current JSON sidecars so the
unified SessionDB contract can be tested without changing persistence behavior.
"""

from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any, Sequence

import api.models as models


_METADATA_FIELDS = frozenset(
    {
        "title",
        "workspace",
        "model",
        "model_provider",
        "created_at",
        "updated_at",
        "pinned",
        "archived",
        "project_id",
        "profile",
        "input_tokens",
        "output_tokens",
        "estimated_cost",
        "cache_read_tokens",
        "cache_write_tokens",
        "personality",
        "active_stream_id",
        "pending_user_message",
        "pending_attachments",
        "pending_started_at",
        "compression_anchor_visible_idx",
        "compression_anchor_message_key",
        "compression_anchor_summary",
        "pre_compression_snapshot",
        "context_engine",
        "compression_anchor_engine",
        "compression_anchor_mode",
        "compression_anchor_details",
        "context_engine_state",
        "context_length",
        "threshold_tokens",
        "last_prompt_tokens",
        "compression_recovery",
        "recommended_recovery_action",
        "compression_recovery_source_session_id",
        "compression_recovery_action",
        "truncation_watermark",
        "truncation_boundary",
        "gateway_routing",
        "gateway_routing_history",
        "llm_title_generated",
        "manual_title",
        "parent_session_id",
        "worktree_path",
        "worktree_branch",
        "worktree_repo_root",
        "worktree_created_at",
        "is_cli_session",
        "source_tag",
        "raw_source",
        "session_source",
        "source_label",
        "read_only",
        "enabled_toolsets",
        "composer_draft",
    }
)

_UNSAFE_FIELDS = frozenset({"session_id", "messages", "tool_calls", "message_count"})


class WebUIJsonSessionDB:
    """Small SessionDB-like facade over existing WebUI session JSON files."""

    backend = "json"
    supports_generation = False
    supports_revision_counter = False
    persists_without_sidecar = False

    def __init__(self, session_dir: Path | str | None = None):
        self._session_dir = Path(session_dir).expanduser().resolve() if session_dir else None

    @property
    def session_dir(self) -> Path:
        return self._session_dir or models.SESSION_DIR

    def list_sessions(self) -> list[dict[str, Any]]:
        """Return compact metadata for persisted WebUI JSON sessions.

        Reads are direct JSON loads and never call ``Session.load()``, because
        that path may self-heal and write repaired transcripts.
        """
        rows: list[dict[str, Any]] = []
        if not self.session_dir.exists():
            return rows
        for path in self.session_dir.glob("*.json"):
            if path.name.startswith("_"):
                continue
            data = self._read_path(path)
            if not isinstance(data, dict):
                continue
            sid = str(data.get("session_id") or path.stem)
            if not models.is_safe_session_id(sid):
                continue
            rows.append(self._metadata_row(sid, data))
        rows.sort(key=lambda row: (bool(row.get("pinned")), self._sort_timestamp(row)), reverse=True)
        return rows

    def read_session(self, sid: str) -> dict[str, Any] | None:
        """Return the full JSON session payload for ``sid`` without mutation."""
        path = self._path_for_sid(sid)
        if path is None or not path.exists():
            return None
        data = self._read_path(path)
        if not isinstance(data, dict):
            return None
        return copy.deepcopy(data)

    def update_metadata(
        self,
        sid: str,
        fields: dict[str, Any],
        *,
        expected_incarnation: int | None = None,
    ) -> dict[str, Any]:
        """Persist metadata fields while preserving messages.

        Unknown top-level keys are accepted and persisted on the sidecar
        dict, matching the SQL backends' ``extra_json`` policy (one metadata
        policy across backends). Only ``_UNSAFE_FIELDS`` (``session_id``,
        ``messages``, ``tool_calls``, ``message_count``) are rejected.

        ``expected_incarnation`` exists for SessionStore signature parity;
        sidecar writes are atomic-replace with no generation/incarnation CAS
        (last-writer-wins), so the token is accepted and unused here.

        Runtime wiring must add Session lock/cache/index parity before using
        it from live WebUI routes.
        """
        if not isinstance(fields, dict):
            raise TypeError("fields must be a dict")
        unsafe = sorted(set(fields) & _UNSAFE_FIELDS)
        if unsafe:
            raise ValueError(f"Unsafe session metadata fields: {', '.join(unsafe)}")

        path = self._existing_path_for_sid(sid)
        data = self._read_writable_session(path)
        data.update(copy.deepcopy(fields))
        data["message_count"] = len(data["messages"])
        self._atomic_write(path, data)
        return self._metadata_row(str(data.get("session_id") or sid), data)

    def archive(
        self,
        sid: str,
        archived: bool = True,
        *,
        expected_incarnation: int | None = None,
    ) -> dict[str, Any]:
        """Set the archived metadata flag without touching transcript messages.

        ``expected_incarnation`` is accepted for SessionStore parity and
        unused (last-writer-wins).
        """
        return self.update_metadata(sid, {"archived": bool(archived)})

    def read_row_version(self, sid: str) -> None:
        """No durable version exists on the JSON backend (last-writer-wins)."""
        return None

    def reconcile_marked_write(
        self,
        sid: str,
        *,
        expected_generation: int,
        expected_incarnation: int,
        fields: dict[str, Any],
    ) -> None:
        """Inert: the atomic reconcile contract is SQL-only. Unreachable in
        production — Session.save() gates the reconcile on
        ``supports_generation``."""
        return None

    def read_metadata_only(self, sid: str) -> dict[str, Any] | None:
        """Metadata-only view of the sidecar (canonical-store contract)."""
        data = self.read_session(sid)
        if data is None:
            return None
        row = self._metadata_row(str(data.get("session_id") or sid), data)
        return row

    def session_exists(self, sid: str) -> bool:
        path = self._path_for_sid(sid)
        return path is not None and path.exists()

    def delete_session(self, sid: str) -> bool:
        """Remove the sidecar (and stale .bak). True if a file existed."""
        path = self._path_for_sid(sid)
        if path is None:
            return False
        existed = path.exists()
        try:
            path.unlink(missing_ok=True)
            path.with_suffix(".json.bak").unlink(missing_ok=True)
        except OSError:
            return False
        return existed

    def lifecycle_delete(
        self, sid: str, *, owner: str, pending_phases: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Delete with retry-ownership semantics (SessionStore contract).

        The JSON backend has no crash-safe lease: sidecars are
        last-writer-wins files, so a failure result here is process-local
        only — the caller retains retry ownership by keeping the file.
        ``pending_phases`` is accepted and ignored for the same reason
        (no durable home for a phase ledger); note ``delete_session``
        already unlinks the sidecar AND the stale .bak, so pure-JSON
        deployments only lose durable retry, not truthfulness.
        """
        path = self._path_for_sid(sid)
        existed = path is not None and path.exists()
        try:
            self.delete_session(sid)
        except OSError as exc:
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "existed": existed,
            }
        if existed and path is not None and path.exists():
            return {"ok": False, "error": "sidecar still present after delete", "existed": True}
        return {"ok": True, "existed": existed}

    def finish_cleanup_phase(
        self, sid: str, phase: str, *, ok: bool, error: str | None = None
    ) -> None:
        """No-op: the JSON backend has no durable phase ledger (the same
        capability boundary as cleanup leases — last-writer-wins files)."""
        return None

    def list_cleanup_phases(self, sid: str | None = None) -> list[dict[str, Any]]:
        """No durable phase ledger on the JSON backend; always empty."""
        return []

    def get_revision(self) -> int:
        """JSON freshness signal: the session directory mtime in ns.

        Sidecar creation/deletion bumps the parent directory mtime; content
        writes go through atomic replace, which also bumps it. This matches
        the cache-invalidation signal models has always used for JSON.
        """
        try:
            return int(getattr(self.session_dir.stat(), "st_mtime_ns", 0))
        except OSError:
            return 0

    def is_active(self) -> bool:
        """JSON is the fallback authority; the store selector decides."""
        return True

    def close(self) -> None:
        return None

    def write_session(
        self,
        session: dict[str, Any],
        *,
        expected_generation: int | None = None,
        expected_incarnation: int | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        """Write a full session payload for tests and migration experiments.

        ``expected_generation``/``expected_incarnation``/``force`` exist for
        SessionStore signature parity; sidecar writes are atomic-replace
        with no generation CAS (last-writer-wins), so they are accepted and
        unused here.
        """
        if not isinstance(session, dict):
            raise TypeError("session must be a dict")
        sid = session.get("session_id")
        path = self._path_for_sid(sid)
        if path is None:
            raise ValueError(f"Unsafe session_id {sid!r}")
        messages = session.get("messages")
        if not isinstance(messages, list):
            raise ValueError("session payload must include a messages list")
        payload = copy.deepcopy(session)
        payload["message_count"] = len(messages)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, payload)
        return copy.deepcopy(payload)

    def _path_for_sid(self, sid: str) -> Path | None:
        if not models.is_safe_session_id(sid):
            return None
        return self.session_dir / f"{sid}.json"

    def _existing_path_for_sid(self, sid: str) -> Path:
        path = self._path_for_sid(sid)
        if path is None:
            raise ValueError(f"Unsafe session_id {sid!r}")
        if not path.exists():
            raise KeyError(sid)
        return path

    def _read_writable_session(self, path: Path) -> dict[str, Any]:
        data = self._read_path(path)
        if not isinstance(data, dict):
            raise ValueError(f"Malformed session JSON: {path.name}")
        sid = data.get("session_id")
        if not models.is_safe_session_id(sid):
            raise ValueError(f"Unsafe session_id {sid!r}")
        if not isinstance(data.get("messages"), list):
            raise ValueError(f"Refusing to write metadata-only session stub: {sid!r}")
        return data

    @staticmethod
    def _read_path(path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    @staticmethod
    def _metadata_row(sid: str, data: dict[str, Any]) -> dict[str, Any]:
        messages = data.get("messages")
        message_count = data.get("message_count")
        if not isinstance(message_count, int):
            message_count = len(messages) if isinstance(messages, list) else 0
        row = {field: copy.deepcopy(data.get(field)) for field in _METADATA_FIELDS if field in data}
        row["session_id"] = sid
        row["message_count"] = message_count
        row["last_message_at"] = data.get("last_message_at") or data.get("updated_at") or data.get("created_at")
        return row

    @staticmethod
    def _sort_timestamp(row: dict[str, Any]) -> float:
        for key in ("last_message_at", "updated_at", "created_at"):
            value = row.get(key)
            if value is None or value == "":
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    @staticmethod
    def _atomic_write(path: Path, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = path.with_suffix(f".tmp.{os.getpid()}.{threading.current_thread().ident}")
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass


def list_sessions() -> list[dict[str, Any]]:
    return WebUIJsonSessionDB().list_sessions()


def read_session(sid: str) -> dict[str, Any] | None:
    return WebUIJsonSessionDB().read_session(sid)


def update_metadata(sid: str, fields: dict[str, Any]) -> dict[str, Any]:
    return WebUIJsonSessionDB().update_metadata(sid, fields)


def archive(sid: str, archived: bool = True) -> dict[str, Any]:
    return WebUIJsonSessionDB().archive(sid, archived)


def write_session(session: dict[str, Any]) -> dict[str, Any]:
    return WebUIJsonSessionDB().write_session(session)
