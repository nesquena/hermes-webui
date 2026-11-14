"""Kind-aware approval registry for self-service installs.

Generalises the skills ownership model (api/skill_ownership.py, section 6 of
docs/user-isolation-design.md) to every surface a non-admin can ask for:

    skill        a user-added skill directory (delegated, see below)
    integration  a Nango provider the user wants enabled
    mcp          an MCP server the user wants added
    cli          a CLI/command the user wants allowed

A user "requests" an item; an admin approves or rejects it from the
governance approvals queue. Requests carry an optional free-form ``payload``
so the requesting surface can keep the details it needs to actually perform
the install once approved (the server command, the provider key, ...).

Storage
-------
STATE_DIR/approvals.json, mapping ``"<kind>:<key>"`` to::

    {
      "kind": "mcp",
      "key": "context7",
      "label": "Context7 MCP",
      "owner_email": "user@example.com",
      "requested_at": <epoch seconds, float>,
      "status": "pending" | "approved" | "rejected",
      "decided_by": "admin@example.com" | null,
      "decided_at": <epoch seconds, float> | null,
      "payload": {...},
      "reason": "why it was rejected" | null
    }

Writes are atomic (same-directory temp file + os.replace) and serialized
under a module lock, matching the other STATE_DIR sidecar files. Reads are
total: a corrupted or unreadable registry degrades to "nothing requested"
instead of raising.

Skills are an ADAPTER, not a copy
---------------------------------
``kind == "skill"`` never touches approvals.json. Every call is delegated to
api/skill_ownership.py so the existing skill flow, its registry file and its
visibility rule stay authoritative and byte-for-byte unchanged; this module
only translates skill_ownership rows into the entry shape above. Skill
entries therefore always carry ``payload == {}`` and ``decided_by/decided_at
== None``.

Approval scope
--------------
An approved ``integration``/``mcp``/``cli`` is approved GLOBALLY (available
to everyone) unless its payload carries ``{"scope": "owner"}``, in which case
it is approved for ``owner_email`` only. Skills follow the skill_ownership
rule (approved == global).

House style: functions raise ValueError (-> 400) and KeyError (-> 404); no
HTTP handler code and no third-party imports live here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

KIND_SKILL = "skill"
KIND_INTEGRATION = "integration"
KIND_MCP = "mcp"
KIND_CLI = "cli"
KIND_GRANT = "grant"
KINDS = (KIND_SKILL, KIND_INTEGRATION, KIND_MCP, KIND_CLI, KIND_GRANT)

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUSES = (STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED)

DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"
DECISIONS = (DECISION_APPROVE, DECISION_REJECT)

# payload["scope"] == "owner" narrows an approval to the requester; anything
# else (or absent) means the approved item is global.
SCOPE_OWNER = "owner"
SCOPE_GLOBAL = "global"

# Admin sentinel from api.ownership.request_owner_scope: an "all" scope sees
# and may use everything.
SCOPE_ALL = "all"

_MAX_KEY_LEN = 512
_MAX_LABEL_LEN = 200

# Requesting is deliberately permission-free (catalog._SELF_ROUTES), so the
# only thing standing between a normal user and an unbounded approvals.json
# is a cap. Both bounds are far above any honest usage; hitting one means
# somebody is flooding the admin queue (which also buries a malicious row
# among thousands of decoys).
_MAX_PENDING_PER_OWNER = 50
_MAX_ENTRIES = 2000

# Grant decisions hold this across the policy write and decide(). Always
# acquire the policy mutation lock first when both are needed. Reentrancy lets
# the existing registry helpers participate in that one decision boundary.
_REGISTRY_LOCK = threading.RLock()


# ── Paths and normalisation ──────────────────────────────────────────────────

def _registry_file() -> Path:
    """Resolve the registry path lazily so test STATE_DIR overrides apply."""
    from api import config

    return Path(config.STATE_DIR) / "approvals.json"


def _norm_kind(kind) -> str:
    """Validate and normalise a kind. Raises ValueError for unknown kinds."""
    value = str(kind or "").strip().lower()
    if value not in KINDS:
        raise ValueError(f"unknown approval kind: {kind!r}")
    return value


def _norm_key(key) -> str:
    """Validate and normalise an item key.

    Keys are opaque per kind (a skill directory key, an MCP server name, a
    Nango provider_config_key, a CLI command). Only characters that would
    corrupt the registry or a log line are rejected here; each surface stays
    responsible for its own stricter identity rules (e.g. the skills path
    validator in governance_api._skill_key_parts).
    """
    value = str(key or "").strip()
    if not value:
        raise ValueError("key required")
    if len(value) > _MAX_KEY_LEN:
        raise ValueError("key too long")
    if any(ch in value for ch in ("\x00", "\n", "\r")):
        raise ValueError("key contains control characters")
    return value


def _norm_email(email) -> str:
    return str(email or "").strip().lower()


def _norm_label(label, fallback: str) -> str:
    value = str(label or "").strip()
    if not value:
        value = fallback
    return value[:_MAX_LABEL_LEN]


def entry_key(kind, key) -> str:
    """Return the registry key (``"<kind>:<key>"``) for an item."""
    return f"{_norm_kind(kind)}:{_norm_key(key)}"


def _payload_dict(payload) -> dict:
    if isinstance(payload, dict):
        # Shallow copy so callers cannot mutate the stored entry afterwards.
        return dict(payload)
    return {}


# ── Registry I/O ─────────────────────────────────────────────────────────────

def load() -> dict:
    """Load the raw registry as {registry_key: entry}. Returns {} on failure.

    Skill entries are never stored here (they live in skill_ownership.json);
    any stray ``skill:`` row is dropped so the delegate stays the single
    source of truth.
    """
    path = _registry_file()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Failed to load approvals registry", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    rows = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        if str(v.get("kind") or "").strip().lower() == KIND_SKILL:
            continue
        if str(k).split(":", 1)[0] == KIND_SKILL:
            continue
        rows[str(k)] = v
    return rows


def save(registry: dict) -> None:
    """Persist the registry with an atomic same-directory replace."""
    path = _registry_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# ── Skill delegation ─────────────────────────────────────────────────────────

def _skill_entry(key: str, raw: dict | None) -> dict | None:
    """Adapt a skill_ownership row into the common entry shape."""
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "").strip().lower()
    if status not in STATUSES:
        status = STATUS_PENDING
    return {
        "kind": KIND_SKILL,
        "key": key,
        "label": key,
        "owner_email": _norm_email(raw.get("owner_email")) or None,
        "requested_at": raw.get("added_at"),
        "status": status,
        "decided_by": None,
        "decided_at": None,
        "payload": {},
        "reason": None,
    }


# ── Public API ───────────────────────────────────────────────────────────────

class PayloadConflict(ValueError):
    """A request reused an existing key with a materially different payload.

    Guards against name squatting: the key is the only thing an admin sees in
    the approvals queue, so a low-privilege user must not be able to park
    ``mcp:context7`` pointing at their own URL and have a later, honest
    request for the same name silently inherit the squatter's payload.
    """

    def __init__(self, entry, message=None):
        super().__init__(message or "a different request already exists under this name")
        self.entry = dict(entry) if isinstance(entry, dict) else {}


# Payload fields that identify WHAT is being installed. A mismatch on any of
# these means the two requests are not the same thing, whatever the key says.
_IDENTITY_FIELDS = ("url", "provider", "command", "endpoint")


def _payload_conflicts(stored, incoming) -> bool:
    """True when incoming names the same key but a different target."""
    if not isinstance(stored, dict) or not isinstance(incoming, dict):
        return False
    for field in _IDENTITY_FIELDS:
        if field not in incoming:
            continue
        old = str(stored.get(field) or "").strip().rstrip("/").lower()
        new = str(incoming.get(field) or "").strip().rstrip("/").lower()
        if old and new and old != new:
            return True
    return False


def request(kind, key, owner_email, label=None, payload=None, *, force=False) -> dict:
    """Record a pending request for ``key`` of ``kind`` owned by the caller.

    Idempotent by design: re-requesting a still-pending item returns the
    existing entry untouched, and requesting an already-approved (or already
    rejected) item is a no-op that returns the stored entry. A request
    therefore never resets a status, reassigns an owner or clears an admin's
    decision.

    Raises :class:`PayloadConflict` when the key already exists with a
    different install target, so the caller can answer 409 instead of silently
    adopting someone else's payload. ``force=True`` (admins only) replaces the
    stored payload and reassigns the row to the forcing caller.
    """
    kind = _norm_kind(kind)
    key = _norm_key(key)
    owner = _norm_email(owner_email)
    if not owner:
        raise ValueError("owner_email required")

    if kind == KIND_SKILL:
        from api import skill_ownership

        raw = skill_ownership.register_skill(key, owner)
        return _skill_entry(key, raw)

    rk = f"{kind}:{key}"
    with _REGISTRY_LOCK:
        registry = load()
        existing = registry.get(rk)
        if isinstance(existing, dict):
            if _payload_conflicts(existing.get("payload"), payload or {}):
                if not force:
                    raise PayloadConflict(existing)
                # Admin override: this row now describes what the admin asked
                # for, and the audit trail records who took it over.
                existing = dict(existing)
                existing["payload"] = dict(payload or {})
                existing["owner_email"] = owner
                existing["label"] = str(label or existing.get("label") or key)
                existing["status"] = STATUS_PENDING
                existing["requested_at"] = time.time()
                registry[rk] = existing
                save(registry)
            return dict(existing)
        # Only NEW rows are capped: an idempotent re-request above already
        # returned, so a caller can never be locked out of an item they
        # already asked for, and an admin decision is never blocked.
        if len(registry) >= _MAX_ENTRIES:
            raise ValueError("the approvals queue is full; ask an admin to clear it")
        pending_for_owner = sum(
            1
            for row in registry.values()
            if isinstance(row, dict)
            and _norm_email(row.get("owner_email")) == owner
            and str(row.get("status") or "").strip().lower() == STATUS_PENDING
        )
        if pending_for_owner >= _MAX_PENDING_PER_OWNER:
            raise ValueError(
                "you already have too many pending requests; wait for an admin to decide them"
            )
        entry = {
            "kind": kind,
            "key": key,
            "label": _norm_label(label, key),
            "owner_email": owner,
            "requested_at": time.time(),
            "status": STATUS_PENDING,
            "decided_by": None,
            "decided_at": None,
            "payload": _payload_dict(payload),
            "reason": None,
        }
        registry[rk] = entry
        save(registry)
        return dict(entry)


def get(kind, key) -> dict | None:
    """Return the entry for ``kind``/``key``, or None when never requested."""
    try:
        kind = _norm_kind(kind)
        key = _norm_key(key)
    except ValueError:
        return None
    if kind == KIND_SKILL:
        from api import skill_ownership

        return _skill_entry(key, skill_ownership.get(key))
    entry = load().get(f"{kind}:{key}")
    return dict(entry) if isinstance(entry, dict) else None


def request_revision(entry: dict) -> str:
    """Bind a delayed review to the exact pending request it examined.

    A reused key, changed owner or updated request context needs a new review.
    Reviewer annotations are excluded so recording advice does not itself
    change the identity of the request.
    """
    view = {key: entry.get(key) for key in
            ("kind", "key", "owner_email", "requested_at", "label")}
    view["payload"] = {key: value for key, value in _payload_dict(entry.get("payload")).items()
                       if key != "auto_review"}
    return hashlib.sha256(json.dumps(view, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def status_of(kind, key) -> str | None:
    """Return 'pending' | 'approved' | 'rejected', or None when unrequested."""
    entry = get(kind, key)
    if not entry:
        return None
    status = str(entry.get("status") or "").strip().lower()
    return status or None


def owner_of(kind, key) -> str | None:
    """Return the lowercased owner email for an item, or None."""
    entry = get(kind, key)
    if not entry:
        return None
    return _norm_email(entry.get("owner_email")) or None


def _kind_filter(kinds) -> tuple:
    """Normalise a kinds filter to a tuple of known kinds.

    ``None`` means "no filter" and returns every kind. An explicit but empty
    (or entirely unknown) filter returns ``()`` and therefore matches nothing:
    a caller that asked for ``?kind=bogus`` gets an empty list rather than the
    whole queue, so the filter fails closed.
    """
    if kinds is None:
        return KINDS
    if isinstance(kinds, str):
        kinds = [kinds]
    wanted = []
    for kind in kinds:
        value = str(kind or "").strip().lower()
        if value in KINDS and value not in wanted:
            wanted.append(value)
    return tuple(wanted)


def _sorted(rows: list) -> list:
    """Oldest-first, with a stable tiebreak so the admin queue never jumps."""
    rows.sort(
        key=lambda r: (
            r.get("requested_at") is None,
            r.get("requested_at") or 0,
            str(r.get("kind") or ""),
            str(r.get("key") or ""),
        )
    )
    return rows


def _skill_rows() -> list:
    """Every skill_ownership row adapted to the common entry shape."""
    from api import skill_ownership

    rows = []
    for key, raw in skill_ownership.load().items():
        entry = _skill_entry(str(key), raw)
        if entry is not None:
            rows.append(entry)
    return rows


def list_pending(kinds=None) -> list:
    """Pending entries across every kind (skills included), oldest first."""
    wanted = _kind_filter(kinds)
    rows = []
    if KIND_SKILL in wanted:
        rows.extend(r for r in _skill_rows() if r.get("status") == STATUS_PENDING)
    for entry in load().values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("kind") or "").strip().lower() not in wanted:
            continue
        if str(entry.get("status") or "").strip().lower() != STATUS_PENDING:
            continue
        rows.append(dict(entry))
    return _sorted(rows)


def list_all(kinds=None, owner_scope=None) -> list:
    """Every entry (any status) across every kind, oldest first.

    ``owner_scope`` follows api.ownership.request_owner_scope: None or 'all'
    returns everything; any other value returns only the entries owned by
    that email, which is what the caller's own /approvals/mine view needs.
    """
    wanted = _kind_filter(kinds)
    scope = _norm_email(owner_scope) if owner_scope is not None else None
    rows = []
    if KIND_SKILL in wanted:
        rows.extend(_skill_rows())
    for entry in load().values():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("kind") or "").strip().lower() not in wanted:
            continue
        rows.append(dict(entry))
    if scope not in (None, "", SCOPE_ALL):
        rows = [r for r in rows if _norm_email(r.get("owner_email")) == scope]
    return _sorted(rows)


def decide(kind, key, decision, decided_by, reason=None) -> dict:
    """Approve or reject a requested item and return the resulting entry.

    Raises ValueError for an unknown kind/decision and KeyError when the item
    was never requested. Rejecting keeps the row (status ``rejected``) so the
    requester can see the outcome and the reason; use remove() to drop it.

    Skills delegate to api/skill_ownership: approve flips the status,
    reject removes the registry entry. Deleting a rejected skill's directory
    is the caller's job (governance_api owns that destructive step) and is
    deliberately not performed here.
    """
    kind = _norm_kind(kind)
    key = _norm_key(key)
    decision = str(decision or "").strip().lower()
    if decision not in DECISIONS:
        raise ValueError(f"invalid decision: {decision!r}")
    decider = _norm_email(decided_by)
    note = str(reason or "").strip() or None
    now = time.time()

    if kind == KIND_SKILL:
        from api import skill_ownership

        raw = skill_ownership.get(key)
        if raw is None:
            raise KeyError(f"unknown approval: {kind}:{key}")
        entry = _skill_entry(key, raw)
        if decision == DECISION_APPROVE:
            skill_ownership.set_status(key, skill_ownership.STATUS_APPROVED)
            entry["status"] = STATUS_APPROVED
        else:
            skill_ownership.remove(key)
            entry["status"] = STATUS_REJECTED
        entry["decided_by"] = decider or None
        entry["decided_at"] = now
        entry["reason"] = note
        return entry

    rk = f"{kind}:{key}"
    with _REGISTRY_LOCK:
        registry = load()
        entry = registry.get(rk)
        if not isinstance(entry, dict):
            raise KeyError(f"unknown approval: {rk}")
        entry["status"] = STATUS_APPROVED if decision == DECISION_APPROVE else STATUS_REJECTED
        entry["decided_by"] = decider or None
        entry["decided_at"] = now
        entry["reason"] = note
        registry[rk] = entry
        save(registry)
        return dict(entry)


def remove(kind, key) -> bool:
    """Delete the entry for ``kind``/``key``. False when it was not there."""
    kind = _norm_kind(kind)
    key = _norm_key(key)
    if kind == KIND_SKILL:
        from api import skill_ownership

        return skill_ownership.remove(key)
    rk = f"{kind}:{key}"
    with _REGISTRY_LOCK:
        registry = load()
        if rk not in registry:
            return False
        del registry[rk]
        save(registry)
        return True


def approval_scope(entry) -> str:
    """Return 'owner' when an entry is approved for its owner only, else 'global'."""
    if not isinstance(entry, dict):
        return SCOPE_GLOBAL
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        return SCOPE_GLOBAL
    scope = str(payload.get("scope") or "").strip().lower()
    return SCOPE_OWNER if scope == SCOPE_OWNER else SCOPE_GLOBAL


def is_approved(kind, key, owner_email=None) -> bool:
    """Whether ``key`` is approved, from ``owner_email``'s point of view.

    An approved item is global unless its payload carries
    ``{"scope": "owner"}``, in which case only the requester (or an 'all'
    admin scope) sees it as approved. Items that were never requested return
    False: the registry only knows about self-service requests, so a surface
    that also has admin-managed globals (skills, MCP servers configured
    directly by an admin) must treat "no entry" as its own pre-existing case
    rather than as a denial.
    """
    entry = get(kind, key)
    if not entry:
        return False
    if str(entry.get("status") or "").strip().lower() != STATUS_APPROVED:
        return False
    if approval_scope(entry) != SCOPE_OWNER:
        return True
    caller = _norm_email(owner_email)
    if caller == SCOPE_ALL:
        return True
    owner = _norm_email(entry.get("owner_email"))
    return bool(caller) and caller == owner


def entry_visible_to_scope(entry, owner_scope) -> bool:
    """Whether an item with registry ``entry`` is visible to a scope.

    Mirrors skill_ownership.entry_visible_to_scope and extends it with the
    owner-scoped approval and the rejected status: an unrequested item
    (entry None) is global, an 'all' scope sees everything, a globally
    approved item is visible to everyone, and anything else (pending,
    rejected, owner-scoped approval) is visible to its owner only.
    """
    if entry is None:
        return True
    if owner_scope == SCOPE_ALL:
        return True
    status = str(entry.get("status") or "").strip().lower()
    if status == STATUS_APPROVED and approval_scope(entry) != SCOPE_OWNER:
        return True
    owner = _norm_email(entry.get("owner_email"))
    scope = _norm_email(owner_scope)
    return bool(scope) and owner == scope
