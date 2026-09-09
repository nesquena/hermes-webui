"""Canonical session-store abstraction.

Both session backends — the JSON sidecar store (``api.webui_session_db``)
and the SQLite store (``api.webui_session_sqlite``) — implement this
contract. ``api.models.get_session_store()`` returns the active backend;
callers route session persistence through it instead of branching on the
backend themselves.

Capabilities
------------
Backends declare capabilities instead of classes: production code branches
on ``supports_generation`` (durable per-session generation CAS + cutover
markers + unreadable-row recovery state), ``supports_revision_counter``
(meta-table revision for cache invalidation), and
``persists_without_sidecar`` (sessions survive with no JSON sidecar on
disk — SQLite/Postgres = True, JSON = False). The last is a topology axis,
distinct from CAS (``supports_generation``) and cache
(``supports_revision_counter``): it tells lifecycle call sites (existence
checks, index rebuilds, lineage scans) that the store is the authoritative
place to look when no sidecar exists. The JSON sidecar backend has none of
the three: its writes are atomic-replace (last writer wins by file
semantics) and its freshness signal is the directory mtime. The ``backend``
tag is informational (logging/tests) only — never a dispatch condition.

Deliberate boundary: ``Session.save()`` keeps its legacy inline JSON writer
for the sidecar backend (field-ordered metadata prefix, legacy-facts
caching, self-heal hooks). The JSON adapter's ``write_session`` is the
canonical CRUD surface used by routing/tests; they are not two competing
writers — the adapter writes whole sidecars, the legacy path writes
field-ordered ones for metadata-only readers.

Backend notes
-------------
- JSON sidecars: one ``<sid>.json`` per session. ``get_revision()`` is the
  session-directory mtime (sidecar create/delete bumps it); ``is_active()``
  is always True (JSON is the fallback authority).
- SQLite: single ``sessions.db``. ``get_revision()`` reads a meta-table
  counter bumped by every write/delete transaction; ``is_active()`` checks
  the durable cutover markers (``created_by=app`` or ``migration_complete``).

A future backend (e.g. Postgres) implements the same surface: ``extra_json``
maps to JSONB, ``get_revision()`` to a sequence, and the cutover markers to
rows in a meta table.
"""
from __future__ import annotations

from typing import Any, Protocol, Sequence

# Canonical filename of the cross-process session-store cutover lock,
# shared by the live WebUI's JSON sidecar writers and the staged
# migration's publish window:
#
#   - api.models._sidecar_write_guard holds it SHARED around every JSON
#     sidecar write (Session.save / Session.save_metadata fallback), with
#     the store-selector re-probe inside the same hold.
#   - scripts/migrate_sessions_to_sqlite.py holds it EXCLUSIVE across the
#     pre-publish source-identity CAS, the atomic sessions.db publication,
#     and sidecar retirement.
#
# A live sidecar save therefore cannot land inside the check→publish window:
# it either completes before the CAS observes the file (drift → the run
# refuses, the newer bytes stay canonical for the next run) or it blocks
# until publication, where the re-probe routes the write into the
# now-authoritative SQL store (cutover adoption). It deliberately lives
# here — the dependency-free contract module — so the server and the
# offline migration script can never drift on the name. It is a separate
# file from .migrating.lock, which serializes whole migration runs and is
# never taken by the live WebUI.
CUTOVER_LOCK_NAME = ".cutover.lock"


class SessionStore(Protocol):
    """Canonical WebUI session persistence contract."""

    def list_sessions(self) -> list[dict[str, Any]]:
        """All session metadata rows, sidebar ordering."""
        ...

    def read_session(self, sid: str) -> dict[str, Any] | None:
        """Full session dict (metadata + messages/tool_calls/context), or None."""
        ...

    def read_metadata_only(self, sid: str) -> dict[str, Any] | None:
        """Metadata fields only — no transcript tables. None if absent."""
        ...

    def write_session(
        self,
        session: dict[str, Any],
        *,
        expected_generation: int | None = None,
        expected_incarnation: int | None = None,
        force: bool = False,
        fresh_incarnation: bool = False,
    ) -> dict[str, Any]:
        """Persist a full session dict; returns the persisted representation.

        SQL backends enforce a durable per-session writer token — the
        (incarnation, generation) pair — backed by a per-SID incarnation
        authority (``session_incarnations``) that survives
        ``delete_session``:

        - Live row: ``expected_generation`` must match the row's generation
          AND ``expected_incarnation`` must match the authority's
          non-retired incarnation (compare-and-bump); a missing/None
          incarnation half, a retired authority, or either mismatch raises
          ``StaleSessionWriteError``. The pair is what discriminates a
          delete + same-SID recreate, which restarts the generation at 1.
          ``force=True`` may overwrite a live row for deliberate
          heals/imports and never consults the authority.
        - Absent row, no authority, no lineage: first create at generation 1
          (``expected_incarnation`` is ignored on every absent-row path —
          the generation lineage plus the retired authority already
          discriminate).
        - Absent row with a non-None ``expected_generation``: the writer
          carries lineage for a session that is gone —
          ``DeletedSessionWriteError``.
        - Absent row, retired authority, no lineage, no lease: the SID was
          deleted; recreating it requires an explicit lease —
          ``RetiredSessionWriteError``. ``force=True`` never inserts an
          absent row.
        - Absent row, retired authority, ``fresh_incarnation=True``: a
          leased recreate — the incarnation counter advances and the new
          row starts at generation 1. Only an explicit recreate API may
          pass this; ``Session.save()``, delete/migration, and heal paths
          never do. A leftover sidecar of the old incarnation is stale and
          is unlinked only by cleanup/delete, never by recreate.

        The returned dict carries the new ``generation`` and the current
        ``incarnation`` (reads JOIN the authority).
        JSON backends accept and ignore the token kwargs (last-writer-wins
        atomic replace); ``fresh_incarnation`` is SQL-only and must not be
        passed to a JSON store.
        """
        ...

    def update_metadata(
        self,
        sid: str,
        fields: dict[str, Any],
        *,
        expected_generation: int | None = None,
        expected_incarnation: int | None = None,
    ) -> dict[str, Any]:
        """Persist a subset of metadata fields without touching the transcript.

        SQL backends bump the session ``generation`` on every call (the same
        version fence as full writes), so a metadata-only writer can never be
        invisible to a concurrent full-save CAS. The writer token is
        required and validated in the writing transaction: both
        ``expected_generation`` and ``expected_incarnation`` must match the
        live row + non-retired authority. A missing row raises
        ``DeletedSessionWriteError``; a None token half, a retired/absent
        authority, or either mismatch raises ``StaleSessionWriteError``
        (fail closed — never a lenient unversioned write). The returned row
        carries both ``generation`` (post-bump) and ``incarnation`` so the
        caller can reseat its lineage.
        The JSON backend has no generation concept: it accepts and ignores
        the token kwargs — last-writer-wins, unchanged.
        """
        ...

    def read_row_version(self, sid: str) -> dict[str, int] | None:
        """Best-effort durable version of the live row + incarnation authority.

        Returns {"generation": int, "incarnation": int} for a live (non-retired)
        row, else None. Read-only; never raises for a healthy-but-absent row.
        JSON backend: returns None (no durable version exists).
        """
        ...

    def reconcile_marked_write(
        self,
        sid: str,
        *,
        expected_generation: int,
        expected_incarnation: int,
        fields: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Atomically reconcile a marked (sidecar-authoritative) owner onto the
        recovered SQL row. In ONE transaction:

        1. Validate the durable version: the live ``sessions`` row must exist,
           its ``generation`` must equal ``expected_generation``, and
           ``session_incarnations`` for the sid must be non-retired with
           ``incarnation == expected_incarnation``.
        2. Apply ONLY ``fields`` (same field-application policy as
           ``update_metadata``: column/JSON classification, ``extra_json``
           merge for unknown keys, ``null_fields_json`` maintenance,
           ``updated_at`` only when explicitly provided).
        3. Bump the session ``generation`` (the same version fence as full
           writes) and the meta revision counter.

        Returns the authoritative FULL row (``read_session(sid)`` shape:
        metadata + messages/tool_calls/context_messages +
        anchor_activity_scenes + new ``generation``) for caller rehydration.
        Returns None on ANY validation mismatch (fail closed; no row was
        modified; the caller must reload/retry). ``fields`` must not contain
        ``session_id``/``messages``/``tool_calls``/``message_count``/
        ``generation`` (ValueError, mirroring ``update_metadata``).

        JSON backend: returns None (contract is SQL-only; unreachable because
        callers gate on ``supports_generation``).
        """
        ...

    def delete_session(self, sid: str) -> bool:
        """Remove the session and all its state. True if a row/file existed."""
        ...

    def lifecycle_delete(
        self, sid: str, *, owner: str, pending_phases: Sequence[str] | None = None
    ) -> dict[str, Any]:
        """Delete the store-owned representation with durable retry ownership.

        Returns ``{"ok": True, "existed": bool}`` on success, or
        ``{"ok": False, "error": str, "existed": bool}`` on failure.

        On failure the store persists a retry lease keyed by ``sid`` (in
        ``cleanup_leases``) recording ``owner`` and the error, so a later
        sweep retains an authoritative retry owner instead of silently
        leaking the row. On success any lease for ``sid`` is cleared. The
        store never touches a representation it does not own (e.g. the
        SQL stores do not unlink JSON sidecars — callers do that only
        after an ``ok`` result).

        ``pending_phases`` (SQL backends): residual-cleanup phase names
        recorded IN THE SAME transaction as the authoritative delete
        (upserted into ``cleanup_phases``; ON CONFLICT preserves
        ``attempts``, refreshes owner/updated_at), so there is no crash
        window between "store deleted" and "residuals durably
        observable". Default None ⇒ exactly the delete + lease behavior
        above. Leases and phases never coexist for a sid: a lease means
        the delete *failed*; phases mean it *succeeded* with residuals
        remaining.

        The JSON sidecar backend has no crash-safe lease or phase ledger
        (last-writer-wins file semantics); it accepts and ignores
        ``pending_phases`` and its failure result is process-local only.
        """
        ...

    def finish_cleanup_phase(
        self, sid: str, phase: str, *, ok: bool, error: str | None = None
    ) -> None:
        """Record the outcome of one residual-cleanup phase attempt.

        ``ok=True`` deletes the ``cleanup_phases`` row; ``ok=False``
        updates ``error``/``updated_at`` and bumps ``attempts``. SQL-only
        durability; the JSON backend is a no-op (callers still run the
        act+verify so behavior is truthful, just not durable).
        """
        ...

    def list_cleanup_phases(self, sid: str | None = None) -> list[dict[str, Any]]:
        """Remaining ``cleanup_phases`` rows for ``sid`` (or every row, for
        a janitor sweep), ordered by session_id, phase. The JSON backend
        returns []."""
        ...

    def archive(
        self,
        sid: str,
        archived: bool = True,
        *,
        expected_generation: int | None = None,
        expected_incarnation: int | None = None,
    ) -> dict[str, Any]:
        """Set the archived flag via ``update_metadata`` — the writer-token
        requirement is inherited (SQL backends require both halves; JSON
        accepts and ignores them)."""
        ...

    def session_exists(self, sid: str) -> bool:
        ...

    def get_revision(self) -> int:
        """Monotonic change counter; every write/delete bumps it.

        List/index caches key their freshness on this instead of filesystem
        mtimes, which SQLite WAL commits do not move.
        """
        ...

    def is_active(self) -> bool:
        """Whether this backend currently holds persistence authority.

        The SQLite backend answers from durable cutover markers so a partial
        migration cannot silently activate it.
        """
        ...

    def close(self) -> None:
        ...
