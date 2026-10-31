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
from typing import Any

import api.models as models


#: Layout version of the sidecars whose writer normalizes EVERY projected
#: metadata key -- and its effective ordering values -- before ``messages``.
#: Emitted by ``Session.save`` as ``_row_layout`` next to ``_mc_v``, i.e. inside
#: the head window. A head/count marker is not enough on its own: a payload can
#: carry ``_mc_v`` and ``anchor_scene_index`` and still serialize title/profile/
#: archived/pinned/raw_source/source_label AFTER ``messages``
#: (``write_session()`` preserves the caller's order), and a cut there would drop
#: every one of them. Only the explicit version vouches that the projected set
#: precedes the cut.
_ROW_LAYOUT_KEY = "_row_layout"
_ROW_LAYOUT_MARKER = f'"{_ROW_LAYOUT_KEY}"'

#: Keys a cut prefix must physically expose before ``messages``. Not every key
#: a row can carry is always persisted: a payload legitimately omits the
#: optional ones (``created_workspace``, token counters, worktree fields...). So
#: admission requires the keys that make a row honest plus the six projected
#: fields whose loss has been measured -- ``title``/``profile``/``archived``/
#: ``pinned``/``raw_source``/``source_label`` -- alongside the ordering values
#: and the count. A missing optional field is absent in the full read too, so it
#: cannot make the prefix and the full row disagree; a missing *required* one is
#: exactly the silent truncation this gate exists to prevent.
_PROJECTED_FIELDS = frozenset(
    {
        "session_id",
        "title",
        "created_at",
        "updated_at",
        "message_count",
        "profile",
        "archived",
        "pinned",
        "raw_source",
        "source_label",
        # An attached ``last_message_at`` is an ordering value the prefix must
        # expose: without it the head reader falls back to ``updated_at`` and
        # disagrees with the full read (re-gate: updated_at=200 vs
        # last_message_at=100).
        "last_message_at",
    }
)


def _head_shows_metadata_first(head: bytes) -> bool:
    """True when the head bytes prove every projected field precedes ``messages``.

    The writer contract ``Session.save`` is what makes the cut safe, and it
    states that contract explicitly: a sidecar whose metadata was normalized
    ahead of ``messages`` carries ``_row_layout`` before that key. Nothing else
    proves it -- ``anchor_scene_index``/``_mc_v`` only vouch for the count and
    the fingerprint, not for the position of the remaining projected fields.

    The marker states the contract, it does not enforce it: a writer that
    preserves the caller's order (``write_session``) can carry a marker it did
    not produce. So the head must ALSO physically show every projected key ahead
    of the cut. Any field that is only discovered after the read cannot be
    recovered from a prefix, and serving the prefix would drop it from the row.
    """
    cut = _metadata_cut_offset(head)
    if cut < 0:
        return False
    prefix = head[:cut]
    if _ROW_LAYOUT_MARKER.encode("ascii") not in prefix:
        return False
    return all(
        f'"{field}"'.encode("ascii") in prefix
        for field in _PROJECTED_FIELDS
        if field != "message_count"
    )


def _metadata_cut_offset(head: bytes) -> int:
    """Offset of the top-level ``messages`` key in a sidecar head, or -1."""
    cut = head.find(b'\n  "messages":')
    if cut < 0:
        cut = head.find(b'"messages":')
    return cut


def _has_complete_row_layout(data: Any) -> bool:
    """True when ``data`` carries the writer's complete-row layout marker.

    A prefix may be served as a row only when the writer stated, in the same
    write, that every projected metadata key precedes ``messages``. The marker
    alone is not enough: the row also needs an integer ``message_count``, since
    the head never sees ``messages`` and ``_metadata_row`` would otherwise fall
    back to ``len(messages)`` == 0.
    """
    return (
        isinstance(data, dict)
        and data.get(_ROW_LAYOUT_KEY) == models._ROW_LAYOUT_VERSION
        and data.get("_mc_v") == models._MESSAGE_COUNT_MARKER
        and isinstance(data.get("message_count"), int)
    )


def _prefix_covers_projected_fields(data: Any) -> bool:
    """True when an already-parsed prefix physically holds every projected key.

    Companion to ``_has_complete_row_layout`` for the budgeted fallback, which
    parses a window of bytes rather than a head: the marker only *states* the
    contract, so a prefix parsed from a document whose caller-supplied order put
    metadata after ``messages`` must not be admitted on the marker alone.
    """
    if not isinstance(data, dict):
        return False
    return all(field in data for field in _PROJECTED_FIELDS if field != "message_count")


_METADATA_FIELDS = frozenset({
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
        "transcript_generation",
        "transcript_generation_baseline",
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
        # Written by the eager hidden-checkpoint path (#7882 / PR #8368). A
        # head-derived sidebar row that drops it reports the pending hidden
        # wakeup as not-yet-excluded and discounts it a second time, which is
        # the exact undercount that field exists to prevent.
        "_pending_hidden_wakeup_at",
    }
)

_UNSAFE_FIELDS = frozenset({"session_id", "messages", "tool_calls", "message_count"})

#: Fields ``_metadata_row`` derives itself, so a cut prefix is only complete
#: when the metadata it needs is already present alongside them.
_ROW_FIELDS = frozenset({"session_id", "message_count", "updated_at", "created_at"})


class WebUIJsonSessionDB:
    """Small SessionDB-like facade over existing WebUI session JSON files."""

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
            data = self._read_metadata_row_source(path)
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

    def update_metadata(self, sid: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Persist allowlisted metadata fields while preserving messages.

        This dormant adapter method is for migration experiments and tests only.
        Runtime wiring must add Session lock/cache/index parity before using it
        from live WebUI routes.
        """
        if not isinstance(fields, dict):
            raise TypeError("fields must be a dict")
        unsafe = sorted((set(fields) & _UNSAFE_FIELDS) | (set(fields) - _METADATA_FIELDS))
        if unsafe:
            raise ValueError(f"Unsafe session metadata fields: {', '.join(unsafe)}")

        path = self._existing_path_for_sid(sid)
        data = self._read_writable_session(path)
        data.update(copy.deepcopy(fields))
        data["message_count"] = len(data["messages"])
        # Same rule as ``write_session``: this writer keeps the caller's (i.e.
        # the existing file's) key order and appends a field it did not have, so
        # it cannot vouch that every projected key precedes ``messages``. A
        # marker inherited from the input would promise an ordering this write
        # did not produce -- measured on the generic writer: a reordered payload
        # carrying the marker served a prefix that lost all six projected
        # fields. The next ``Session.save`` re-mints it.
        data.pop(_ROW_LAYOUT_KEY, None)
        self._atomic_write(path, data)
        return self._metadata_row(str(data.get("session_id") or sid), data)

    def archive(self, sid: str, archived: bool = True) -> dict[str, Any]:
        """Set the archived metadata flag without touching transcript messages."""
        return self.update_metadata(sid, {"archived": bool(archived)})

    def write_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """Write a full session payload for tests and migration experiments."""
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
        # This writer normalizes ``message_count`` and the projected layout only:
        # it deep-copies the caller dict, then moves every key the row reader
        # projects ahead of ``messages`` in the order ``Session.save`` uses for
        # them. The layout marker promises that ALL projected keys precede the
        # cut, so a marker inherited from the input would vouch for a layout this
        # writer did not produce -- measured: a reordered payload carrying the
        # marker lost title/profile/archived/pinned/raw_source/source_label from
        # the row. Minting it here would be equally wrong, because the caller's
        # remaining keys keep their order, so the marker is retired. Callers that
        # normalize the complete layout themselves (``Session.save``) keep their
        # own marker.
        payload.pop(_ROW_LAYOUT_KEY, None)
        payload["message_count"] = len(messages)
        payload = self._normalize_row_layout(payload)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, payload)
        return copy.deepcopy(payload)

    def _normalize_row_layout(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Re-emit every head-contract key ahead of ``messages``, caller order kept.

        ``write_session`` copies the caller's dict, so a payload can serialize
        the six projected fields after ``messages``. Moving them ahead (and
        ``messages`` after them) makes the ``Session.save`` ordering contract
        true for this writer's own output, so the full reader sees the same
        values either way and a future reader cannot cut the fields away.
        ``message_count`` keeps its value; only its position moves with the
        ordering values. ``_mc_v`` moves with it: the count marker promises
        that the count it vouches for sits in the bounded prefix
        (``_prefix_message_count``), so leaving it behind ``messages`` would
        mint the same kind of unkept promise this writer is retiring.
        """
        if "messages" not in payload:
            return payload
        head: dict[str, Any] = {}
        tail: dict[str, Any] = {}
        for key, value in payload.items():
            if key in _PROJECTED_FIELDS or key == "_mc_v":
                head[key] = value
            else:
                tail[key] = value
        ordered = dict(head)
        ordered.update(tail)
        return ordered

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

    #: Bytes read from the head of a sidecar when listing session rows. The
    #: metadata keys are serialized before ``messages`` (see ``Session.save``),
    #: so they all live in the first few KB: measured over 637 real sidecars the
    #: top-level ``"messages"`` key started at p50 2.6 KB, p99 32 KB, max 43 KB.
    _METADATA_HEAD_BYTES = 64 * 1024

    @classmethod
    def _read_metadata_head(cls, path: Path) -> dict[str, Any] | None:
        """Parse only the metadata that precedes ``messages`` in a sidecar.

        ``list_sessions()`` consumes ~20 scalar fields per session, but a full
        ``json.loads`` also decodes every message: on this store's 637 sidecars
        (1.18 GB) that costs ~14 s per refresh, spent entirely on bytes the
        sidebar never reads. Metadata is serialized before ``messages``, so the
        document is cut at that key and closed with ``}`` -- a few KB instead of
        megabytes.

        Returns ``None`` when the cut is unavailable (key missing, or a metadata
        prefix that overflowed the window), so the caller can fall back to
        ``models._read_metadata_json_prefix`` or a full parse.
        """
        try:
            with open(path, "rb") as fh:
                head = fh.read(cls._METADATA_HEAD_BYTES)
        except OSError:
            return None
        # The cut is only a shortcut under the writer's layout: when the head
        # does not prove every metadata key precedes ``messages``, refuse it and
        # let the caller read the document whole.
        if not _head_shows_metadata_first(head):
            return None
        cut = _metadata_cut_offset(head)
        prefix = head[:cut].rstrip()
        # Drop any trailing separator so the object closes cleanly.
        while prefix and prefix[-1:] in (b",", b"\n", b"\r", b"\t", b" "):
            prefix = prefix[:-1]
        try:
            data = json.loads(prefix.decode("utf-8") + "\n}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    @classmethod
    def _read_metadata_row_source(cls, path: Path) -> dict[str, Any] | None:
        """Read only the fields ``_metadata_row`` consumes, for ``list_sessions``.

        ``_metadata_row`` derives ``message_count`` from ``len(messages)`` when
        the field is absent. The head read never sees ``messages``, so a sidecar
        without an integer ``message_count`` must be parsed whole -- otherwise
        the sidebar would report 0 messages for it.

        Both admission paths are gated on the SAME proof, ``_ROW_LAYOUT_KEY``
        before the cut. ``_mc_v``/``anchor_scene_index``/``message_count`` only
        vouch for the count and the fingerprint: a payload can carry all three
        and still serialize title/profile/archived/pinned/raw_source/
        source_label after ``messages`` (``write_session()`` preserves the
        caller's order). Admitting either prefix on a narrower condition
        silently drops those fields from the row, so nothing but the explicit
        layout marker admits a prefix -- any other shape takes the full read.
        """
        data = cls._read_metadata_head(path)
        if not _has_complete_row_layout(data):
            # Either metadata overflowed the head window (measured worst case: a
            # 73 KB ``compression_anchor_summary``, #4633), the layout is not the
            # writer's, or the file carries no marker. Any head that failed the
            # layout test is discarded here, so a partial prefix can never be
            # served as a row. models' budgeted reader stages up to 1 MiB and
            # also stops at ``messages``, so an oversized prefix still avoids a
            # multi-megabyte parse; it is the fallback rather than the default
            # because its tokenizer scans the whole window once per stop key
            # instead of stopping at a byte offset -- and it is held to the same
            # layout proof.
            #
            # The shared reader opens the file itself, so this fallback needs
            # the same error boundary the head read has: a sidecar removed or
            # unreadable after enumeration must skip its own row, never raise
            # out of the whole ``list_sessions()``.
            data = None
            try:
                raw_prefix = models._read_metadata_json_prefix(path)
            except (OSError, ValueError):
                return None
            fallback = None
            if raw_prefix:
                try:
                    fallback = json.loads(raw_prefix)
                except (json.JSONDecodeError, ValueError):
                    fallback = None
            if (
                _has_complete_row_layout(fallback)
                and "messages" not in fallback
                and _ROW_FIELDS.issubset(fallback)
            ):
                data = fallback
        if (
            isinstance(data, dict)
            and _has_complete_row_layout(data)
            and "messages" not in data
            and _prefix_covers_projected_fields(data)
        ):
            return data
        # Unmarked, incomplete or ambiguous layout: the full reader is the only
        # one that sees every field regardless of key order.
        return cls._read_path(path)

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
