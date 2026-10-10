"""Pure, read-only aggregate audit for lifecycle metadata (issue #498 shadow slice).

Maintainer-approved slice: disabled-by-default (master flag + mode == shadow),
aggregate-only, read-only shadow comparison plus offline drift audit. No writes,
no events, no cache invalidation, no index/state.db mutation, no stream
behavior, no apply/yes/migration/repair path.

See https://github.com/nesquena/hermes-webui/issues/498#issuecomment-5385677557
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from api.agent_sessions import _is_continuation_session as _canonical_is_continuation
from api.agent_sessions import normalize_agent_session_source
from api.agent_sessions import open_state_db_readonly


def _strict_lifecycle_value(value) -> bool | None:
    """Strict tri-state normalizer for untrusted lifecycle inputs.

    Permitted encodings only:
      - actual bool -> bool(value)
      - exact int 0/1 -> False/True
      - exact float 0.0/1.0 -> False/True
    Everything else (strings including "true"/"false"/"" / whitespace /
    yes/no/on/off, objects, arrays, null/None, non-0/1 numbers) -> None
    (unknown, fail-closed). Never uses generic truthiness.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    if isinstance(value, float):
        if value == 1.0:
            return True
        if value == 0.0:
            return False
        return None
    return None


def provisional_truth(json_pinned, json_archived, core_pinned, core_archived) -> dict:
    """Pure hypothetical planner per approved truth table.

    archived = json_archived or core_archived
    pinned   = (json_pinned or core_pinned) and not archived

    Inputs are first strict-normalized to the tri-state contract (bool and
    exact 0/1 incl. 0.0/1.0 -> False/True; strings/objects/arrays/null and
    non-0/1 numbers -> None). Unknown (None) is treated as False for the
    hypothetical only after strict normalization. Never writes.
    """
    ja_raw = _strict_lifecycle_value(json_archived)
    ca_raw = _strict_lifecycle_value(core_archived)
    jp_raw = _strict_lifecycle_value(json_pinned)
    cp_raw = _strict_lifecycle_value(core_pinned)
    ja = ja_raw if ja_raw is not None else False
    ca = ca_raw if ca_raw is not None else False
    jp = jp_raw if jp_raw is not None else False
    cp = cp_raw if cp_raw is not None else False
    archived = ja or ca
    pinned = (jp or cp) and not archived
    return {"archived": archived, "pinned": pinned}


def read_core_lifecycle_batch(db_path: Path, sids: set[str]) -> dict[str, dict]:
    """Read pinned/archived tri-state for exact ids, read-only.

    Preserves distinction:
      - absent row            -> {"pinned": None, "archived": None, "exists": False}
      - present row, col absent or NULL -> pinned/archived None, exists True
      - actual bool, exact int 0/1, exact float 0.0/1.0 -> False/True
      - any other integer, non-0/1 float, string (including "true"/"false"/""
        / whitespace / yes/no/on/off), object, array, null -> None
        (unknown, fail-closed). Never uses generic truthiness.
      - database/schema/query read failure -> {"unreadable": True} (distinct from ordinary absent row -> {"exists": False, "unreadable": False})
    Never mutates disk. Uses read-only URI helper.
    """
    wanted = {s for s in (sids or set()) if isinstance(s, str) and s.strip()}
    result: dict[str, dict] = {sid: {"pinned": None, "archived": None, "exists": False, "unreadable": False} for sid in wanted}
    if not wanted:
        return result
    db_path = Path(db_path)
    if not db_path.exists():
        for sid in wanted:
            result[sid]["unreadable"] = True
        return result
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            try:
                cur.execute("PRAGMA table_info(sessions)")
                cols = {row[1] for row in cur.fetchall()}
            except Exception:
                for sid in wanted:
                    result[sid]["unreadable"] = True
                return result
            if "id" not in cols:
                for sid in wanted:
                    result[sid]["unreadable"] = True
                return result
            has_pinned = "pinned" in cols
            has_archived = "archived" in cols
            ids = list(wanted)
            for i in range(0, len(ids), 500):
                chunk = ids[i : i + 500]
                placeholders = ",".join("?" * len(chunk))
                pinned_expr = "s.pinned" if has_pinned else "NULL AS pinned"
                archived_expr = "s.archived" if has_archived else "NULL AS archived"
                try:
                    cur.execute(
                        f"SELECT s.id, {pinned_expr}, {archived_expr} FROM sessions s WHERE s.id IN ({placeholders})",
                        chunk,
                    )
                except Exception:
                    for sid in chunk:
                        if sid in result:
                            result[sid]["unreadable"] = True
                    continue
                try:
                    rows = cur.fetchall()
                except Exception:
                    for sid in chunk:
                        if sid in result:
                            result[sid]["unreadable"] = True
                    continue
                for row in rows:
                    sid = row["id"]
                    if not isinstance(sid, str) or not sid.strip():
                        continue
                    if sid not in result:
                        continue
                    result[sid]["exists"] = True
                    for col, has_col in (("pinned", has_pinned), ("archived", has_archived)):
                        if not has_col:
                            result[sid][col] = None
                        else:
                            result[sid][col] = _strict_lifecycle_value(row[col])
    except Exception:
        for sid in wanted:
            if not result[sid].get("unreadable"):
                result[sid]["unreadable"] = True
        pass
    return result


def _tri_state_sidecar_flag(data: dict, key: str):
    """Read pinned/archived as tri-state via established core sidecar path.

    Permitted encodings only: actual bool and exact numeric 0/1 (including
    exact 0.0/1.0) -> False/True. Every string -> None (including "true",
    "false", "", whitespace, yes/no/on/off, arbitrary strings). Objects,
    arrays, null, and non-0/1 numeric values also -> None (unknown,
    fail-closed). Preserves absent vs explicit false. Never coerces unknown
    via bool(raw).
    """
    if key not in data:
        return None
    return _strict_lifecycle_value(data.get(key))


def _classify_pending(data: dict) -> bool | None:
    """Tri-state pending classification for offline audit (fail-closed).

    Known active (True) -> blocks as active lineage.
    Known inactive (False) -> allows comparison.
    Unknown/malformed (None) -> blocks as ambiguous.

    Legitimate indicators preserved without generic truthiness:
      - active_stream_id: active iff non-empty stripped string; inactive iff
        absent/None/"" / whitespace; ambiguous for any non-string type.
      - pending_user_message: same as active_stream_id.
      - has_pending_user_message: active iff True/1/1.0/"true"/"1";
        inactive iff absent/None/False/0/0.0/"false"/"0"/""/whitespace;
        ambiguous otherwise (e.g., "maybe", 2, [], {}).
    If any field is ambiguous, overall is ambiguous (fail-closed) even if
    another field is active. This is distinct from lifecycle strict
    normalization and never uses bool(value) on strings.
    """
    has_ambiguous = False
    has_active = False
    # active_stream_id
    if "active_stream_id" in data:
        v = data.get("active_stream_id")
        if v is None:
            pass
        elif isinstance(v, str):
            if v.strip():
                has_active = True
            else:
                pass
        else:
            has_ambiguous = True
    # pending_user_message
    if "pending_user_message" in data:
        v = data.get("pending_user_message")
        if v is None:
            pass
        elif isinstance(v, str):
            if v.strip():
                has_active = True
            else:
                pass
        else:
            has_ambiguous = True
    # has_pending_user_message
    if "has_pending_user_message" in data:
        v = data.get("has_pending_user_message")
        if v is None:
            pass
        elif isinstance(v, bool):
            if v is True:
                has_active = True
            else:
                pass
        elif isinstance(v, int) and not isinstance(v, bool):
            if v == 1:
                has_active = True
            elif v == 0:
                pass
            else:
                has_ambiguous = True
        elif isinstance(v, float):
            if v == 1.0:
                has_active = True
            elif v == 0.0:
                pass
            else:
                has_ambiguous = True
        elif isinstance(v, str):
            s = v.strip().lower()
            if not s:
                pass
            elif s in {"true", "1"}:
                has_active = True
            elif s in {"false", "0"}:
                pass
            else:
                has_ambiguous = True
        else:
            has_ambiguous = True
    if has_ambiguous:
        return None
    if has_active:
        return True
    return False


def _is_sidecar_pending(data: dict) -> bool:
    """Legacy wrapper: returns True only for known active pending state."""
    state = _classify_pending(data)
    return state is True


def _classify_parent_ref(raw) -> tuple[str, str | None]:
    """Classify an untrusted parent reference.

    Returns (kind, valid_id):
      - absent  (raw is None / missing): valid root/no-parent
      - valid   (non-empty non-whitespace str): exact parent identity (raw exact string)
      - malformed (empty/whitespace/non-string/BLOB/numeric): fail-closed
    Callers must treat absent and valid as non-blocking; malformed makes
    the owning candidate a blocked anchor. Valid ids preserve raw exact string
    without stripping or coercion.
    """
    if raw is None:
        return ("absent", None)
    if isinstance(raw, str):
        if raw and raw.strip():
            return ("valid", raw)
        return ("malformed", None)
    return ("malformed", None)


def _is_trusted_core_source(row: dict) -> bool:
    raw = row.get("source")
    if raw is None:
        raw = row.get("raw_source") or row.get("session_source")
    meta = normalize_agent_session_source(raw)
    return str(meta.get("session_source") or "").strip().lower() == "webui"


def _inventory_sidecars(session_dir: Path, profile: str) -> tuple[dict[str, dict], dict, set[str], bool, int]:
    out: dict[str, dict] = {}
    blocked: dict[str, int] = {"malformed": 0, "id_mismatch": 0, "messages_invalid": 0, "profile_mismatch": 0}
    blocked_ids: set[str] = set()
    unanchorable_blocked = 0
    session_dir = Path(session_dir)
    sidecar_unreadable = False
    entries: list[Path] = []

    def _record_blocked(kind: str, *raw_ids) -> None:
        """Record rejected inventory evidence without double-counting anchors.

        An admitted raw identity is counted later through its blocked lineage.
        Only a rejected record with no admissible identity can seed the aggregate
        ambiguity accumulator directly.
        """
        nonlocal sidecar_unreadable, unanchorable_blocked
        try:
            blocked[kind] += 1
            anchors = {
                raw_id
                for raw_id in raw_ids
                if isinstance(raw_id, str) and raw_id and raw_id.strip()
            }
            if anchors:
                blocked_ids.update(anchors)
            else:
                unanchorable_blocked += 1
        except Exception:
            sidecar_unreadable = True

    try:
        if not session_dir.exists() or not session_dir.is_dir():
            return out, blocked, blocked_ids, True, unanchorable_blocked
        try:
            entries = list(session_dir.glob("*.json"))
        except Exception:
            return out, blocked, blocked_ids, True, unanchorable_blocked
    except Exception:
        return out, blocked, blocked_ids, True, unanchorable_blocked
    for p in entries:
        if p.name.startswith("_") or p.name.startswith("."):
            continue
        file_stem_raw = p.stem
        file_sid_early = file_stem_raw if isinstance(file_stem_raw, str) and file_stem_raw.strip() else ""
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            sidecar_unreadable = True
            continue
        try:
            data = json.loads(text)
        except Exception:
            _record_blocked("malformed", file_sid_early)
            continue
        if not isinstance(data, dict):
            _record_blocked("malformed", file_sid_early)
            continue
        file_sid_raw = p.stem
        raw_payload_sid = data.get("session_id")
        if not isinstance(raw_payload_sid, str) or not raw_payload_sid.strip():
            _record_blocked("malformed", file_sid_raw, raw_payload_sid)
            continue
        payload_sid = raw_payload_sid
        file_sid = file_sid_raw if isinstance(file_sid_raw, str) and file_sid_raw.strip() else ""
        if not file_sid:
            _record_blocked("malformed", payload_sid)
            continue
        if payload_sid != file_sid:
            _record_blocked("id_mismatch", file_sid, payload_sid)
            continue
        sid = payload_sid
        if not sid:
            _record_blocked("malformed", file_sid)
            continue
        prof = data.get("profile")
        if not isinstance(prof, str) or not prof.strip() or prof != profile:
            _record_blocked("profile_mismatch", sid)
            continue
        msgs = data.get("messages")
        if not isinstance(msgs, list):
            _record_blocked("messages_invalid", sid)
            continue
        try:
            pinned = _tri_state_sidecar_flag(data, "pinned")
            archived = _tri_state_sidecar_flag(data, "archived")
            msg_count = len(msgs)
            pending_state = _classify_pending(data)
            raw_parent_val = data.get("parent_session_id")
            started_at = data.get("started_at")
            if started_at is None:
                started_at = data.get("created_at")
            if started_at is None:
                started_at = 0
            kind, valid_parent = _classify_parent_ref(raw_parent_val)
        except Exception:
            sidecar_unreadable = True
            continue
        if kind == "malformed":
            _record_blocked("malformed", sid)
            continue
        out[sid] = {
            "session_id": sid,
            "pinned": pinned,
            "archived": archived,
            "message_count": msg_count,
            "is_active": pending_state,
            "parent_session_id": valid_parent,
            "started_at": started_at,
            "raw_parent": raw_parent_val,
            "raw_started_at": started_at,
        }
    if sidecar_unreadable:
        return out, blocked, blocked_ids, True, unanchorable_blocked
    return out, blocked, blocked_ids, False, unanchorable_blocked


def _inventory_core_all(db_path: Path) -> tuple[dict[str, dict], bool, int, set[str]]:
    """Inventory core rows with strict ID validation and error signaling.

    Returns (out, had_unreadable_error, invalid_id_blocked_count, malformed_parent_anchor_ids).
    Only non-empty, non-whitespace Python str ids are admitted.
    NULL, int, BLOB, empty, whitespace are blocked and counted as invalid.
    Any SELECT/connection failure sets had_unreadable_error True (fail-closed).
    For a malformed parent reference that survives SQLite retrieval (non-string,
    empty/whitespace), the owning trusted id is recorded as a blocked anchor so
    a matching sidecar cannot be classified as matched. Raw values are not
    surfaced. Note: SQLite TEXT affinity may canonicalize integer literals into
    text before Python sees them; tests use a no-affinity schema to exercise raw
    numeric/BLOB rejection.
    """
    out: dict[str, dict] = {}
    invalid_blocked = 0
    malformed_parent_anchors: set[str] = set()
    db_path = Path(db_path)
    if not db_path.exists():
        return out, False, 0, malformed_parent_anchors
    try:
        with closing(open_state_db_readonly(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.cursor()
            try:
                cur.execute("PRAGMA table_info(sessions)")
                cols = {row[1] for row in cur.fetchall()}
            except Exception:
                return out, True, 0, malformed_parent_anchors
            if "id" not in cols:
                return out, False, 0, malformed_parent_anchors
            has_pinned = "pinned" in cols
            has_archived = "archived" in cols
            has_parent = "parent_session_id" in cols
            has_started = "started_at" in cols
            has_ended = "ended_at" in cols
            has_end_reason = "end_reason" in cols
            has_source = "source" in cols
            has_session_source = "session_source" in cols
            pinned_expr = "s.pinned" if has_pinned else "NULL AS pinned"
            archived_expr = "s.archived" if has_archived else "NULL AS archived"
            parent_expr = "s.parent_session_id" if has_parent else "NULL AS parent_session_id"
            started_expr = "s.started_at" if has_started else "NULL AS started_at"
            ended_expr = "s.ended_at" if has_ended else "NULL AS ended_at"
            end_reason_expr = "s.end_reason" if has_end_reason else "NULL AS end_reason"
            source_expr = "s.source" if has_source else "NULL AS source"
            session_source_expr = "s.session_source" if has_session_source else "NULL AS session_source"
            try:
                cur.execute(
                    f"SELECT s.id, {pinned_expr}, {archived_expr}, {parent_expr}, "
                    f"{started_expr}, {ended_expr}, {end_reason_expr}, {source_expr}, {session_source_expr} "
                    f"FROM sessions s"
                )
            except Exception:
                return out, True, 0, malformed_parent_anchors
            for row in cur.fetchall():
                raw_id = row["id"]
                if not isinstance(raw_id, str) or not raw_id.strip():
                    invalid_blocked += 1
                    continue
                sid = raw_id
                pinned = _strict_lifecycle_value(row["pinned"]) if has_pinned else None
                if not has_pinned or row["pinned"] is None:
                    # _strict already handles None -> None, but preserve column absent as None
                    if not has_pinned:
                        pinned = None
                    elif row["pinned"] is None:
                        pinned = None
                archived = _strict_lifecycle_value(row["archived"]) if has_archived else None
                if not has_archived or row["archived"] is None:
                    if not has_archived:
                        archived = None
                    elif row["archived"] is None:
                        archived = None
                raw_source = str(row["source"]).strip().lower() if row["source"] else None
                raw_session_source = str(row["session_source"]).strip().lower() if row["session_source"] else None
                trusted = _is_trusted_core_source({"source": raw_source, "session_source": raw_session_source})
                parent_val = row["parent_session_id"]
                kind, valid_parent = _classify_parent_ref(parent_val)
                if kind == "malformed":
                    # Preserve internal blocked anchor for the valid core id; do not surface raw value
                    malformed_parent_anchors.add(sid)
                    parent_sid = None
                else:
                    parent_sid = valid_parent
                out[sid] = {
                    "session_id": sid,
                    "pinned": pinned,
                    "archived": archived,
                    "parent_session_id": parent_sid,
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "end_reason": str(row["end_reason"]).strip().lower() if isinstance(row["end_reason"], str) and row["end_reason"].strip() else (str(row["end_reason"]).strip().lower() if row["end_reason"] else None) if row["end_reason"] else None,
                    "source": raw_source,
                    "session_source": raw_session_source,
                    "trusted": bool(trusted),
                    "malformed_parent": kind == "malformed",
                }
            return out, False, invalid_blocked, malformed_parent_anchors
    except Exception:
        return out, True, invalid_blocked, malformed_parent_anchors
    return out, False, invalid_blocked, malformed_parent_anchors


def _rows_for_canonical_continuation(sidecars: dict[str, dict], core_all: dict[str, dict]) -> dict[str, dict]:
    """Build rows_by_id shape expected by the canonical continuation predicate.

    Core lineage facts (ended_at/end_reason/started_at/source/parent_session_id)
    are authoritative for continuation checks; a core row's canonical parent
    (including None) is never overwritten by sidecar evidence. Sidecar values
    only fill provenance gaps (started_at/ended_at/end_reason/source) and add
    sidecar-only candidates.
    """
    rows: dict[str, dict] = {}
    for sid, v in core_all.items():
        rows[sid] = {
            "id": sid,
            "parent_session_id": v.get("parent_session_id"),
            "started_at": v.get("started_at"),
            "ended_at": v.get("ended_at"),
            "end_reason": v.get("end_reason"),
            "source": v.get("source"),
            "session_source": v.get("session_source"),
        }
    for sid, v in sidecars.items():
        if sid not in rows:
            rows[sid] = {
                "id": sid,
                "parent_session_id": v.get("parent_session_id"),
                "started_at": v.get("started_at"),
                "ended_at": None,
                "end_reason": None,
                "source": None,
                "session_source": None,
            }
        else:
            for k in ("started_at", "ended_at", "end_reason", "source", "session_source"):
                if rows[sid].get(k) is None and v.get(k) is not None:
                    rows[sid][k] = v.get(k)
    return rows


def _canonical_continuation_root(rows_by_id: dict[str, dict], sid: str) -> str:
    cur = sid
    seen: dict[str, int] = {cur: 0}
    path: list[str] = [cur]
    for _ in range(len(rows_by_id) + 5):
        row = rows_by_id.get(cur)
        parent_id = row.get("parent_session_id") if row else None
        if not isinstance(parent_id, str) or not parent_id.strip():
            return cur
        parent = rows_by_id.get(parent_id)
        if not parent:
            return cur
        try:
            is_cont = bool(_canonical_is_continuation(parent, row))
        except Exception:
            return cur
        if not is_cont:
            return cur
        if parent_id in seen:
            idx = seen[parent_id]
            cycle = path[idx:]
            try:
                return min(cycle)
            except Exception:
                return cur
        cur = str(parent_id)
        seen[cur] = len(path)
        path.append(cur)
    return cur


def _validate_profile(profile) -> str:
    if not isinstance(profile, str) or not profile.strip():
        raise ValueError("profile is required: pass a non-empty --profile")
    return profile


def compute_aggregate_diagnostics(session_dir: Path, db_path: Path, profile) -> dict:
    """Aggregate-only, profile-scoped, fail-closed diagnostics.

    Never surfaces titles, prompts, transcript, or session IDs — only counts
    and the profile name. Profile is required and must be non-empty; None/empty
    fails closed. Cross-profile sidecars and unverified core sources are
    blocked, never compared.
    """
    profile = _validate_profile(profile)
    inv_result: tuple = _inventory_sidecars(session_dir, profile)
    sidecar_unreadable = False
    unanchorable_sidecar_blocked = 0
    if len(inv_result) == 5:
        sidecars, inv_blocked, inv_blocked_ids, sidecar_unreadable, unanchorable_sidecar_blocked = inv_result
    elif len(inv_result) == 4:
        sidecars, inv_blocked, inv_blocked_ids, sidecar_unreadable = inv_result
    elif len(inv_result) == 3:
        sidecars, inv_blocked, inv_blocked_ids = inv_result
        sidecar_unreadable = False
    else:
        sidecars, inv_blocked = inv_result
        inv_blocked_ids = set()
        sidecar_unreadable = False
    if sidecar_unreadable:
        blocked_unreadable_sidecar = 1
    else:
        blocked_unreadable_sidecar = 0
    db_path = Path(db_path)
    db_exists = db_path.exists()
    core_all: dict[str, dict] = {}
    blocked_unreadable = 0
    blocked_ambiguous_schema = 0
    invalid_core_blocked = 0
    core_had_error = False
    if not db_exists:
        blocked_unreadable = len(sidecars) + sum(inv_blocked.values()) if (sidecars or any(inv_blocked.values())) else 1
    else:
        inv_core = _inventory_core_all(db_path)
        malformed_core_parent_anchors: set[str] = set()
        if isinstance(inv_core, tuple) and len(inv_core) == 4:
            core_all, core_had_error, invalid_core_blocked, malformed_core_parent_anchors = inv_core
        elif isinstance(inv_core, tuple) and len(inv_core) == 3:
            core_all, core_had_error, invalid_core_blocked = inv_core
        elif isinstance(inv_core, tuple) and len(inv_core) == 2:
            core_all, core_had_error = inv_core
        else:
            core_all = inv_core if isinstance(inv_core, dict) else {}
        if core_had_error:
            blocked_unreadable = len(sidecars) + sum(inv_blocked.values()) + invalid_core_blocked if (sidecars or any(inv_blocked.values()) or invalid_core_blocked) else 1
        try:
            with closing(open_state_db_readonly(db_path)) as conn:
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
                if cur.fetchone() is None:
                    if not core_had_error:
                        blocked_unreadable = len(sidecars) + sum(inv_blocked.values()) + invalid_core_blocked if (sidecars or any(inv_blocked.values()) or invalid_core_blocked) else 1
                else:
                    cur.execute("PRAGMA table_info(sessions)")
                    cols = {row[1] for row in cur.fetchall()}
                    if "id" not in cols:
                        if not core_had_error:
                            blocked_ambiguous_schema = len(sidecars) + sum(inv_blocked.values()) + invalid_core_blocked if (sidecars or any(inv_blocked.values()) or invalid_core_blocked) else 1
        except Exception:
            if not core_had_error:
                blocked_unreadable = len(sidecars) + sum(inv_blocked.values()) + invalid_core_blocked if (sidecars or any(inv_blocked.values()) or invalid_core_blocked) else 1

    inv_blocked_total = sum(inv_blocked.values()) + invalid_core_blocked

    # Filter foreign/untrusted core-only rows outside the eligible comparison
    # domain before lineage construction: a core row with no matching sidecar
    # and an unverified provenance is not an eligible lineage and must not
    # inflate totals or blocked counts. A matching unverified row is retained
    # and blocks fail-closed at the lineage loop below. Rejected/malformed
    # sidecar identities (inv_blocked_ids) are retained as blocked anchors so a
    # trusted core row with the same id is not misclassified as core_only.
    blocked_anchor_ids = set(inv_blocked_ids) | set(malformed_core_parent_anchors if 'malformed_core_parent_anchors' in locals() else set())
    core_eligible = {
        sid: row
        for sid, row in core_all.items()
        if bool(row.get("trusted")) or sid in sidecars or sid in blocked_anchor_ids
    }
    rows_by_id = _rows_for_canonical_continuation(sidecars, core_eligible)
    _audit_blocked: set[str] = set()
    for _sid, _row in list(rows_by_id.items()):
        _pid = _row.get("parent_session_id")
        if not isinstance(_pid, str) or not _pid.strip():
            continue
        _parent = rows_by_id.get(_pid)
        if not _parent:
            continue
        try:
            if _canonical_is_continuation(_parent, _row) and _parent.get("ended_at") is None:
                _audit_blocked.add(_sid)
                _audit_blocked.add(str(_pid))
        except Exception:
            # The canonical predicate should receive normalized rows, but an
            # unexpected malformed value must block rather than split into a
            # clean comparison bucket.
            _audit_blocked.add(_sid)
            _audit_blocked.add(str(_pid))
    try:
        _state: dict[str, int] = {}
        _cycles: list[list[str]] = []
        for _start in list(rows_by_id.keys()):
            if _state.get(_start, 0) != 0:
                continue
            _stack: list[str] = []
            _pos: dict[str, int] = {}
            _cur: str | None = _start
            while _cur is not None:
                _st = _state.get(_cur, 0)
                if _st == 1:
                    _idx = _pos.get(_cur, 0)
                    _cycle = _stack[_idx:]
                    if _cycle:
                        _cycles.append(list(_cycle))
                    break
                if _st == 2:
                    break
                _state[_cur] = 1
                _pos[_cur] = len(_stack)
                _stack.append(_cur)
                _row_cur = rows_by_id.get(_cur)
                _pid_cur = _row_cur.get("parent_session_id") if _row_cur else None
                _parent_cur = rows_by_id.get(_pid_cur) if isinstance(_pid_cur, str) and _pid_cur.strip() else None
                _is_cont = False
                try:
                    _is_cont = bool(_parent_cur and _canonical_is_continuation(_parent_cur, _row_cur))
                except Exception:
                    # Fail closed if a hostile row reaches the canonical
                    # predicate: both ends of the observed relationship are
                    # ambiguous audit anchors.
                    _audit_blocked.add(_cur)
                    if isinstance(_pid_cur, str):
                        _audit_blocked.add(_pid_cur)
                    break
                if not _is_cont:
                    break
                _cur = str(_pid_cur) if isinstance(_pid_cur, str) else None
                if len(_stack) > len(rows_by_id) + 5:
                    break
            for _n in _stack:
                _state[_n] = 2
        for _c in _cycles:
            if not _c:
                continue
            _audit_blocked.update(_c)
    except Exception:
        # A traversal failure cannot be allowed to downgrade malformed
        # ancestry into a normal aggregate bucket.
        _audit_blocked.update(rows_by_id)
    blocked_anchor_ids = blocked_anchor_ids | _audit_blocked
    # Build deterministic total order for representatives: (started_at,
    # continuation depth, stable id tie-break) derived from canonical
    # rows_by_id facts. Depth is continuation distance from root.
    def _started_float_for_rank(sid: str) -> float:
        try:
            return float((rows_by_id.get(sid) or {}).get("started_at") or 0)
        except Exception:
            return 0.0

    def _continuation_depth(sid: str) -> int:
        depth = 0
        cur = sid
        seen = {cur}
        for _ in range(len(rows_by_id) + 5):
            row = rows_by_id.get(cur)
            parent_id = row.get("parent_session_id") if row else None
            parent = rows_by_id.get(parent_id) if parent_id else None
            if not parent or not _canonical_is_continuation(parent, row):
                break
            if parent_id in seen:
                break
            depth += 1
            cur = str(parent_id)
            seen.add(cur)
        return depth

    def _rep_key(sid: str) -> tuple:
        return (_started_float_for_rank(sid), _continuation_depth(sid), str(sid))

    # Blocked anchors participate in lineage grouping so an exact-ID trusted
    # core row is grouped with its rejected sidecar and blocked, not counted as
    # core_only. Anchors never contribute sidecar facts to mismatch counts.
    # Also propagate blocked ancestry: any valid descendant reached by exact raw
    # parent references from a blocked anchor must also be blocked, even where
    # canonical continuation cannot link through the malformed parent.
    # Union of all valid exact parent_session_id references from both sidecar
    # and core evidence for each candidate (admission precheck, not canonical
    # continuation semantics).
    blocked_expanded: set[str] = set(blocked_anchor_ids)
    all_candidate_parents: dict[str, set[str]] = defaultdict(set)
    for sid in set(sidecars.keys()) | set(core_eligible.keys()):
        if sid in sidecars:
            p = sidecars[sid].get("parent_session_id")
            if isinstance(p, str) and p.strip():
                all_candidate_parents[sid].add(p)
        if sid in core_eligible:
            p2 = core_eligible[sid].get("parent_session_id")
            if isinstance(p2, str) and p2.strip():
                all_candidate_parents[sid].add(p2)
    changed = True
    while changed:
        changed = False
        for sid, parents in list(all_candidate_parents.items()):
            if sid in blocked_expanded:
                continue
            if any(pr in blocked_expanded for pr in parents):
                blocked_expanded.add(sid)
                changed = True
    all_ids = set(sidecars.keys()) | set(core_eligible.keys()) | blocked_expanded
    # Ensure blocked anchors have rows_by_id entries for canonical root calc.
    for bid in blocked_expanded:
        if bid not in rows_by_id:
            rows_by_id[bid] = {
                "id": bid,
                "parent_session_id": None,
                "started_at": 0,
                "ended_at": None,
                "end_reason": None,
                "source": None,
                "session_source": None,
            }
    lineage_roots: dict[str, set[str]] = defaultdict(set)
    for sid in all_ids:
        root = _canonical_continuation_root(rows_by_id, sid)
        lineage_roots[root].add(sid)

    total_lineages = len(lineage_roots)
    matched = 0
    pinned_jt_cf = 0
    pinned_jf_ct = 0
    pinned_conflict = 0
    archived_jt_cf = 0
    archived_jf_ct = 0
    archived_conflict = 0
    sidecar_only_empty = 0
    sidecar_only_msgful = 0
    core_only = 0
    blocked_active = 0
    # Anchored sidecar rejections are counted once when their blocked lineage
    # is visited below. Only unanchorable sidecar records and invalid core IDs
    # without an admissible lineage identity seed the aggregate directly.
    blocked_ambiguous = invalid_core_blocked + unanchorable_sidecar_blocked

    effective_unreadable = blocked_unreadable or blocked_unreadable_sidecar
    if sidecar_unreadable:
        return {
            "profile": profile,
            "total_lineages": 0,
            "total_sidecar_lineages": 0,
            "total_core_lineages": 0,
            "matched": 0,
            "pinned_mismatch": {"json_true_core_false": 0, "json_false_core_true": 0, "conflict": 0},
            "archived_mismatch": {"json_true_core_false": 0, "json_false_core_true": 0, "conflict": 0},
            "sidecar_only": {"empty": 0, "messageful": 0},
            "core_only": 0,
            "blocked": {"active": 0, "unreadable": 1, "ambiguous": 0},
        }
    if blocked_unreadable or blocked_ambiguous_schema or core_had_error:
        return {
            "profile": profile,
            "total_lineages": total_lineages if total_lineages else (len(sidecars) + inv_blocked_total if (sidecars or inv_blocked_total) else 1),
            "total_sidecar_lineages": len({_canonical_continuation_root(rows_by_id, sid) for sid in sidecars.keys()}) if sidecars else 0,
            "total_core_lineages": len({_canonical_continuation_root(rows_by_id, sid) for sid in core_eligible.keys()}) if core_eligible else 0,
            "matched": 0,
            "pinned_mismatch": {"json_true_core_false": 0, "json_false_core_true": 0, "conflict": 0},
            "archived_mismatch": {"json_true_core_false": 0, "json_false_core_true": 0, "conflict": 0},
            "sidecar_only": {"empty": 0, "messageful": 0},
            "core_only": 0,
            "blocked": {"active": 0, "unreadable": blocked_unreadable, "ambiguous": (blocked_ambiguous_schema or blocked_ambiguous) + (0 if blocked_unreadable else 0)},
        }

    for _root, members in lineage_roots.items():
        # Any lineage touching a blocked anchor (malformed sidecar id) is
        # blocked fail-closed, even if a trusted core row shares that id.
        if any(mid in blocked_expanded for mid in members):
            blocked_ambiguous += 1
            continue
        side_members = [m for m in members if m in sidecars]
        core_members = [m for m in members if m in core_eligible]
        # Tri-state pending: ambiguous -> blocked ambiguous, active -> blocked active
        has_ambiguous_pending = any(sidecars[mid].get("is_active") is None for mid in side_members)
        if has_ambiguous_pending:
            blocked_ambiguous += 1
            continue
        is_active = any(sidecars[mid].get("is_active") is True for mid in side_members)
        if is_active:
            blocked_active += 1
            continue
        has_side = bool(side_members)
        has_core = bool(core_members)
        has_trusted_core = any(core_eligible[mid].get("trusted") for mid in core_members)
        has_untrusted_core = any(not core_eligible[mid].get("trusted") for mid in core_members)
        if has_untrusted_core:
            blocked_ambiguous += 1
            continue
        has_unknown_lifecycle = False
        for _mid in side_members:
            if sidecars[_mid].get("pinned") is None or sidecars[_mid].get("archived") is None:
                has_unknown_lifecycle = True
                break
        if not has_unknown_lifecycle:
            for _mid in core_members:
                if core_eligible[_mid].get("trusted") and (core_eligible[_mid].get("pinned") is None or core_eligible[_mid].get("archived") is None):
                    has_unknown_lifecycle = True
                    break
        if has_unknown_lifecycle:
            blocked_ambiguous += 1
            continue
        if has_side and not has_core:
            max_msgs = max(sidecars[mid].get("message_count", 0) for mid in side_members)
            if max_msgs == 0:
                sidecar_only_empty += 1
            else:
                sidecar_only_msgful += 1
            continue
        if has_core and not has_side:
            if not has_trusted_core:
                blocked_ambiguous += 1
                continue
            core_only += 1
            continue
        if has_side and has_core and not has_trusted_core:
            blocked_ambiguous += 1
            continue
        rep_side_id = max(side_members, key=_rep_key)
        rep_core_id = max(core_members, key=_rep_key)
        side_pinned = sidecars[rep_side_id].get("pinned")
        side_archived = sidecars[rep_side_id].get("archived")
        core_pinned = core_eligible[rep_core_id].get("pinned")
        core_archived = core_eligible[rep_core_id].get("archived")
        if core_pinned is None or core_archived is None or side_pinned is None or side_archived is None:
            blocked_ambiguous += 1
            continue
        pinned_match = side_pinned == core_pinned
        archived_match = side_archived == core_archived
        if pinned_match and archived_match:
            matched += 1
        else:
            if not pinned_match:
                if side_pinned and not core_pinned:
                    pinned_jt_cf += 1
                elif not side_pinned and core_pinned:
                    pinned_jf_ct += 1
            if not archived_match:
                if side_archived and not core_archived:
                    archived_jt_cf += 1
                elif not side_archived and core_archived:
                    archived_jf_ct += 1
            if not pinned_match and not archived_match:
                pinned_conflict += 1
                archived_conflict += 1

    return {
        "profile": profile,
        "total_lineages": total_lineages,
        "total_sidecar_lineages": len({_canonical_continuation_root(rows_by_id, sid) for sid in sidecars.keys()}) if sidecars else 0,
        "total_core_lineages": len({_canonical_continuation_root(rows_by_id, sid) for sid in core_eligible.keys()}) if core_eligible else 0,
        "matched": matched,
        "pinned_mismatch": {"json_true_core_false": pinned_jt_cf, "json_false_core_true": pinned_jf_ct, "conflict": pinned_conflict},
        "archived_mismatch": {"json_true_core_false": archived_jt_cf, "json_false_core_true": archived_jf_ct, "conflict": archived_conflict},
        "sidecar_only": {"empty": sidecar_only_empty, "messageful": sidecar_only_msgful},
        "core_only": core_only,
        "blocked": {"active": blocked_active, "unreadable": blocked_unreadable, "ambiguous": blocked_ambiguous},
    }


def shadow_compare(session_dir: Path, db_path: Path, profile) -> dict:
    """Explicit/offline alias for the dormant shadow comparison (offline-only).

    Aggregate-only, read-only helper that delegates to
    compute_aggregate_diagnostics. It is not wired to any runtime path
    (no sidebar, API, watcher, or stream integration) and has no
    runtime caller in this slice; it exists solely for explicit offline
    use via tests and scripts/audit_session_metadata_sync.py.
    """
    return compute_aggregate_diagnostics(session_dir, db_path, profile)
