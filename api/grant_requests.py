"""Governance denial -> admin approvals bridge (kind "grant").

The agent-side tool gate spools every grantable governance denial into
``~/.hermes/webui/governance-grant-requests.json`` (see
hermes_cli.dashboard_governance.grant_requests). This module ingests that
spool into the api/approvals registry so denials show up in the admins'
governance screen as pending rows with the other self-service kinds, and it
applies an approved request to the governance policy document.

Ingest rules: a spool item becomes a pending registry row once; while the row
is pending its payload count/last_seen keeps tracking the spool. A decided
row (approved or rejected) is never re-created by ingest, so a rejection
stays quiet even when the user keeps hitting the denial.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_SPOOL_NAME = "governance-grant-requests.json"


def _spool_path() -> Path:
    """Resolve the spool lazily from config.STATE_DIR so a test override never
    reads or writes the live admin queue."""
    from api import config

    return Path(config.STATE_DIR) / _SPOOL_NAME

# gkind -> (top-level grants section, nested path) applied on approval.
_GRANT_TARGETS = {
    "skill": ("skills", ("view", "load")),
    # Editing one skill (skill_manage). Approving adds the name to
    # skills.manage only; viewing and loading it are a separate grant.
    "skill_manage": ("skills", ("manage",)),
    "cli": ("cli", ("commands",)),
    "workdir": ("cli", ("workdir_roots",)),
    "file_read": ("files", ("read_roots",)),
    "file_write": ("files", ("write_roots",)),
    "mcp": ("mcp", ("servers",)),
    "tool": ("tools", ("builtins",)),
    "toolset": ("tools", ("toolsets",)),
    "profile": ("profiles", ()),
    "workspace": ("workspaces", ()),
    # An API route the caller was blocked from. Approving adds the path to the
    # user's routes allowlist; the permission layer still applies, so a route
    # grant alone never confers an admin capability.
    "route": ("routes", ()),
    # A governance permission the caller will be stopped by next. Only the
    # names in GRANTABLE_PERMISSIONS are ever applied here; see that list for
    # why this is an allowlist and not the route guard's denylist.
    "permission": ("permissions", ()),
    # A secret-bearing path a governed user was blocked from by a denied glob.
    # Approving adds the exact path to files.allow_globs, a per-person exception
    # that overrides denied_globs for that one path (see the engine tool_policy).
    "secret_glob": ("files", ("allow_globs",)),
}

# The ONLY permissions a one-click decision may ever write into a policy.
#
# Added 28 Aug 2026 with the related-suggestion review detail (ticket 10). The
# obvious guard was to reuse _route_is_requestable's shape below (refuse
# governance:* and *:admin) but that predicate is safe only BECAUSE the
# permission wall still stands behind a route grant. Handing out the permission
# itself removes that wall, and two names slip straight through a denylist of
# that shape: terminal:use is the RCE surface the catalog deliberately split off
# chat:use, and config:write is a body-sink permission (api/settings_scope.py)
# that /api/settings admits at config:read, so granting it opens every settings
# write with no second wall left at all.
#
# So this is an allowlist, and it holds read-shaped permissions only. Anything
# that writes, restarts, schedules or executes is reported to the administrator
# as information ("this is also needed") and has to be granted by editing the
# access rules, where the whole entry is in view.
GRANTABLE_PERMISSIONS = frozenset({
    "analytics:read",
    "config:read",
    "cron:read",
    "dashboard:read",
    "files:read",
    "gateway:read",
    "git:read",
    "integrations:read",
    "kanban:read",
    "logs:read",
    "mcp:read",
    "model:read",
    "plugins:read",
    "profiles:read",
    "sessions:read",
    "skills:read",
    "status:read",
    "system:read",
    "todos:read",
})


def _load_spool() -> dict:
    try:
        data = json.loads(_spool_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def _request_label(item: dict) -> str:
    gkind = str(item.get("gkind") or "")
    value = str(item.get("value") or "")
    labels = {
        "cli": "CLI command",
        "skill": "Skill",
        "skill_manage": "Skill editing",
        "workdir": "CLI workdir",
        "file_read": "File read access",
        "file_write": "File write access",
        "mcp": "MCP server",
        "tool": "Tool",
        "toolset": "Toolset",
        "profile": "Profile",
        "workspace": "Workspace",
        "route": "API route",
        "secret_glob": "Secret file access",
    }
    if gkind == "permission":
        # The requester sees this label in Settings > Access requests, where a
        # permission slug means nothing. Use the plain sentence the approvals
        # screen already shows for that permission, and fall back to the bare
        # kind rather than putting the slug in front of an end user.
        try:
            from api.capability_risk import PERMISSION_RISKS

            capability = str((PERMISSION_RISKS.get(value) or {}).get("capability") or "").strip()
        except Exception:
            capability = ""
        return f"Access: {capability}" if capability else "Access"
    return f"{labels.get(gkind, gkind)}: {value}"


def _permission_is_grantable(permission: str) -> bool:
    """Whether a permission may be written by a one-click approval at all.

    Deliberately an allowlist (GRANTABLE_PERMISSIONS). Enforced HERE, not only
    in the surface that offers the suggestion, so a hand-crafted POST naming
    terminal:use or config:write cannot reach the policy through this path.
    """
    name = str(permission or "").strip()
    if not name or name not in GRANTABLE_PERMISSIONS:
        return False
    # Belt and braces: the two shapes _route_is_requestable already refuses can
    # never be in the allowlist, and must stay refused if one is ever added.
    return not (name.startswith("governance:") or name.endswith(":admin"))


def _route_is_requestable(path: str, method: str) -> bool:
    """A route denial is one-click-grantable only when it is not an admin or
    governance-mutation surface. Those stay hard-denied: a route grant adds the
    path to the caller's allowlist, and while the permission layer still guards
    the action, we do not even offer governance/admin routes in the queue."""
    try:
        from api.governance.catalog import route_permission
    except Exception:
        return False
    for m in ("GET", method.upper()):
        perm = route_permission(path, m) or ""
        if perm.startswith("governance:") or perm.endswith(":admin"):
            return False
    return True


def record_route_denial(email: str, path: str, method: str = "GET") -> bool:
    """Spool a route_not_allowed denial as a grantable request (kind route).

    Route denials happen at the HTTP layer, not the agent tool layer, so the
    engine's tool-denial spool never sees them; this is the webui-side writer.
    Writes the same spool the engine uses, so ingest_spool turns it into a
    pending Access row. Best-effort, never raises, admin routes excluded."""
    try:
        email = str(email or "").strip().lower()
        path = (str(path or "").split("?", 1)[0]).strip()
        if not email or not path or not path.startswith("/api/"):
            return False
        if not _route_is_requestable(path, method):
            return False
        key = f"{email}|route|{path}"
        now = time.time()
        spool_file = _spool_path()
        spool_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = spool_file.with_suffix(".lock")
        with open(lock_path, "w") as lock_fh:
            try:
                import fcntl
                fcntl.flock(lock_fh, fcntl.LOCK_EX)
            except Exception:
                pass
            spool = _load_spool()
            entry = spool.get(key)
            if isinstance(entry, dict):
                entry["count"] = int(entry.get("count") or 0) + 1
                entry["last_seen"] = now
            else:
                spool[key] = {
                    "email": email, "gkind": "route", "value": path,
                    "tool": "", "reason": "route_not_allowed", "detail": path,
                    "count": 1, "first_seen": now, "last_seen": now,
                }
            tmp = spool_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(spool, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(spool_file)
        return True
    except Exception as exc:  # pragma: no cover
        logger.debug("route denial spool failed: %s", exc)
        return False


def _notify_admins_of_request(entry: dict) -> bool:
    """Tell administrators out of band that a new access request is waiting.

    Reported 27 Aug 2026 ("Let administrators approve governance requests
    through Chat or Telegram"): an admin had to be looking at the WebUI to
    notice a request at all. This delivers the awareness half: a summary and
    where to decide.

    It deliberately does NOT carry an approve or deny action. A decision that
    grants capability must be bound to a verified administrator identity, and
    a chat message is forwardable, spoofable and replayable: acting on one
    would let anybody holding the message grant access. Remote decisions need
    a signed, single-use, identity-bound action token and an audited callback,
    which is a security design decision rather than a wiring job. Until that
    exists, the notification points at the Approvals tab, where the decision is
    already authenticated and audited.

    Never raises; a failed notification is logged and leaves the request
    untouched in the queue.
    """
    try:
        from api.config import load_settings

        destination = str((load_settings() or {}).get("governance_alert_destination") or "").strip()
        if not destination:
            return False
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        trigger = str(payload.get("trigger") or "").strip()
        lines = [
            "SynthPulse access request waiting for a decision.",
            f"Requester: {entry.get('owner_email') or 'unknown'}",
            f"Requested: {entry.get('label') or entry.get('key') or 'unknown'}",
        ]
        if trigger:
            lines.append(f"Asked for: {trigger}")
        lines.append("Decide in the SynthPulse WebUI: Governance > Approvals (kind: Access).")
        from api.cron_webui_delivery import deliver_external_notice

        ok, error = deliver_external_notice(destination, "\n".join(lines))
        if not ok:
            logger.info("governance request notification not delivered: %s", error)
        return bool(ok)
    except Exception as exc:  # pragma: no cover: never break ingest
        logger.debug("governance request notification failed: %s", exc)
        return False


def notify_requester_of_decision(entry: dict, decision: str, decided_by: str, reason: str = "") -> bool:
    """Tell the person who asked that their request was decided.

    Reported 27 Aug 2026 ("Notify users when a governance approval is granted
    or denied"): a requester had to keep reopening the Access requests screen
    to find out. This closes the loop out of band.

    The destination is per person, configured by an admin under the settings
    key ``governance_user_alert_destinations`` as ``{email: "platform:chat_id"}``.
    Somebody without one simply does not get an out-of-band message; the
    decision is still visible in their own Access requests list, which stays
    the authoritative place. Nothing here grants or changes anything: it is a
    message about a decision that has already been persisted.

    Never raises; a failed notification leaves the decision untouched.
    """
    try:
        requester = str(entry.get("owner_email") or "").strip().lower()
        if not requester:
            return False
        from api.config import load_settings

        settings = load_settings() or {}
        destinations = settings.get("governance_user_alert_destinations")
        if not isinstance(destinations, dict):
            return False
        destination = ""
        for email, value in destinations.items():
            if str(email or "").strip().lower() == requester:
                destination = str(value or "").strip()
                break
        if not destination:
            return False
        granted = str(decision or "").strip().lower() == "approve"
        what = str(entry.get("label") or entry.get("key") or "your request")
        lines = [
            f"Your SynthPulse access request was {'approved' if granted else 'declined'}.",
            f"Requested: {what}",
            f"Decided by: {decided_by or 'an administrator'}",
        ]
        if str(reason or "").strip():
            lines.append(f"Note: {str(reason).strip()}")
        lines.append(
            "It is active now, no need to ask again."
            if granted
            else "Reply to your administrator if you still need this."
        )
        from api.cron_webui_delivery import deliver_external_notice

        ok, error = deliver_external_notice(destination, "\n".join(lines))
        if not ok:
            logger.info("governance decision notice not delivered: %s", error)
        return bool(ok)
    except Exception as exc:  # pragma: no cover: never break a decision
        logger.debug("governance decision notification failed: %s", exc)
        return False


def materialise_suggested_grant(
    email: str,
    gkind: str,
    value: str,
    *,
    origin_key: str = "",
    confidence: str = "",
    signal: str = "",
) -> str | None:
    """Turn an approved related suggestion into a real pending grant row.

    Returns the spool-shaped key (``email|gkind|value``, WITHOUT the registry's
    ``grant:`` prefix) so the caller can hand it straight to the ordinary grant
    decision handler, which owns the policy write, the audit and the sync. None
    means the same item was already decided and must not be applied twice.

    Writes the registry directly under the registry lock, exactly as
    ingest_spool does, instead of going through approvals.request(). That is on
    purpose: request() enforces two anti-flood bounds (a per-owner pending cap
    and a global entry cap) which exist to stop a user burying the admin queue
    in self-service asks. Neither should be able to make an ADMINISTRATOR
    unable to act on a review they already have open. Do not "simplify" this
    back into request().
    """
    from api import approvals

    email = str(email or "").strip().lower()
    gkind = str(gkind or "").strip()
    value = str(value or "").strip()
    if not email or not value or gkind not in _GRANT_TARGETS:
        return None
    if gkind == "permission" and not _permission_is_grantable(value):
        return None
    skey = f"{email}|{gkind}|{value}"
    rk = f"{approvals.KIND_GRANT}:{skey}"
    with approvals._REGISTRY_LOCK:
        registry = approvals.load()
        entry = registry.get(rk)
        if entry is not None:
            return skey if str(entry.get("status") or "pending") == "pending" else None
        registry[rk] = {
            "kind": approvals.KIND_GRANT,
            "key": skey,
            "label": _request_label({"gkind": gkind, "value": value}),
            "owner_email": email,
            "status": "pending",
            "requested_at": time.time(),
            "payload": {
                "email": email,
                "gkind": gkind,
                "value": value,
                # Not a denial the person hit: an administrator added it while
                # reviewing a request of theirs. The reason says so rather than
                # borrowing a denial name the engine never wrote.
                "reason": "suggested_dependency",
                "tool": "",
                "detail": "",
                "trigger": "",
                "count": 1,
                "origin_key": str(origin_key or ""),
                "confidence": str(confidence or ""),
                "signal": str(signal or ""),
            },
        }
        approvals.save(registry)
    # No _notify_admins_of_request here: an administrator is looking at this
    # queue right now, which is where the row came from.
    return skey


def _operation_payloads(item: dict, email: str, gkind: str, value: str) -> dict:
    """Carry only explicit operation provenance, never arbitrary spool fields.

    An operation is evidence, not permission to execute. Resume consumers must
    revalidate identity, freshness, policy, opt-in and the canonical run state.
    """
    operations = item.get("operations")
    if not isinstance(operations, dict):
        return {}
    fields = ("version", "operation_id", "actor_email", "gkind", "value", "tool", "reason",
              "session_id", "request_id", "tool_call_id", "binding_status", "prompt_sha256",
              "freshness_status", "trigger_redacted", "created_at")
    result = {}
    for op_id, op in list(operations.items())[:128]:
        if not isinstance(op, dict) or op.get("version") != 1 or op.get("operation_id") != op_id:
            continue
        if (op.get("actor_email"), op.get("gkind"), op.get("value")) != (email, gkind, value):
            continue
        result[op_id] = {name: op[name] for name in fields if name in op}
    return result


def ingest_spool(owner_scope: str | None = None) -> int:
    """Sync the denial spool into the approvals registry. Returns how many
    pending grant rows exist afterwards. Never raises.

    A requester view supplies its verified owner email so reading that view
    ingests only that person's denials. The administrator queue omits the
    scope and continues to ingest every owner.
    """
    try:
        from api import approvals

        spool = _load_spool()
        owner = str(owner_scope).strip().lower() if owner_scope is not None else None
        pending = 0
        for skey, item in spool.items():
            if not isinstance(item, dict):
                continue
            email = str(item.get("email") or "").strip().lower()
            if owner is not None and email != owner:
                continue
            gkind = str(item.get("gkind") or "")
            value = str(item.get("value") or "")
            if not email or gkind not in _GRANT_TARGETS or not value:
                continue
            rk = f"{approvals.KIND_GRANT}:{skey}"
            notification = None
            with approvals._REGISTRY_LOCK:
                registry = approvals.load()
                entry = registry.get(rk)
                payload = {
                    "email": email,
                    "gkind": gkind,
                    "value": value,
                    "reason": str(item.get("reason") or ""),
                    "tool": str(item.get("tool") or ""),
                    "detail": str(item.get("detail") or ""),
                    # What the person actually asked for, redacted and
                    # truncated by the engine before it was ever stored
                    # (27 Aug 2026 ticket). Empty when the surface could not
                    # supply it: never a guess.
                    "trigger": str(item.get("trigger") or ""),
                    "count": int(item.get("count") or 1),
                    "operations": _operation_payloads(item, email, gkind, value),
                    "operations_overflow": item.get("operations_overflow") is True,
                }
                if entry is None:
                    registry[rk] = {
                        "kind": approvals.KIND_GRANT,
                        "key": skey,
                        "label": _request_label(item),
                        "owner_email": email,
                        "status": "pending",
                        "requested_at": float(item.get("first_seen") or 0.0),
                        "payload": payload,
                    }
                    approvals.save(registry)
                    pending += 1
                    # Reserve notification only for the ingest that created the
                    # row; network delivery must not hold the registry lock.
                    notification = dict(registry[rk])
                elif str(entry.get("status") or "pending") == "pending":
                    # Polling the queue must not erase why automatic review
                    # deferred to a person. Advice belongs only to the exact
                    # request reviewed; changed context needs a fresh review.
                    prior_payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
                    if (isinstance(prior_payload.get("auto_review"), dict)
                            and approvals.request_revision(entry) == approvals.request_revision({**entry, "payload": payload})):
                        payload["auto_review"] = prior_payload["auto_review"]
                    entry["payload"] = payload
                    approvals.save(registry)
                    pending += 1
            if notification is not None:
                # 14-09-2026: a person with approval: automatic gets the
                # administrator's rules applied first; administrators are only
                # pinged when the reviewer leaves the row to them.
                from api import grant_auto_review
                grant_auto_review.schedule(notification, on_manual=_notify_admins_of_request)
        return pending
    except Exception as exc:  # pragma: no cover: queue must render regardless
        logger.debug("grant request ingest failed: %s", exc)
        return 0


def drop_from_spool(spool_key: str) -> None:
    """Remove one decided item from the spool (best-effort)."""
    try:
        spool = _load_spool()
        if spool_key in spool:
            spool.pop(spool_key)
            spool_file = _spool_path()
            tmp = spool_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(spool, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, spool_file)
    except Exception as exc:
        logger.debug("grant request spool drop failed: %s", exc)


def _secret_glob_values(value: str) -> list[str]:
    """Allow-glob entries for an approved secret path, in the form the engine matches.

    The engine compares absolute paths with fnmatch, so a literal ``~/...`` never
    matched and a bare directory only matched the directory itself, never the
    files in it: an approved folder kept raising file_denied_glob (Yaser, IvCB
    and CCC key folders, 05-10-2026). Expand ``~`` to the real home and add
    ``<dir>/**`` for a directory (an existing one, or a request ending in ``/``).
    """
    import os, pwd
    raw = value.strip()
    home = pwd.getpwuid(os.getuid()).pw_dir
    path = home + raw[1:] if raw == "~" or raw.startswith("~/") else raw
    is_dir = raw.endswith("/") or os.path.isdir(path)
    path = path.rstrip("/") or "/"
    if any(ch in path for ch in "*?["):
        return [path]
    return [path, path + "/**"] if is_dir else [path]


def apply_grant_to_policy(raw: dict, payload: dict) -> tuple[str, str] | None:
    """Apply an approved grant request to the raw policy document in place.

    Returns (before, after) audit strings, or None when the payload does not
    describe an applicable grant. The grant lands on the USER entry (never a
    group or role), so approving a request widens exactly one person.
    """
    email = str(payload.get("email") or "").strip().lower()
    gkind = str(payload.get("gkind") or "")
    value = str(payload.get("value") or "")
    target = _GRANT_TARGETS.get(gkind)
    if not email or not value or target is None:
        return None
    if gkind == "permission" and not _permission_is_grantable(value):
        # The last gate before the policy document. A permission outside the
        # allowlist is refused here even when something upstream offered it.
        return None
    section, subkeys = target
    values = [value]
    if gkind == "secret_glob":
        values = _secret_glob_values(value)
    users = raw.setdefault("users", {})
    user = users.get(email)
    if user is None:
        # Only grant to users the policy already knows; an unknown email is
        # an ingest artefact, not a reason to create a policy entry.
        return None
    grants = user.setdefault("grants", {})
    added = []
    if not subkeys:
        lst = grants.setdefault(section, [])
        if value not in lst:
            lst.append(value)
            added.append(f"{section}+{value}")
    else:
        sec = grants.setdefault(section, {})
        for sub in subkeys:
            lst = sec.setdefault(sub, [])
            for item in values:
                if item not in lst:
                    lst.append(item)
                    added.append(f"{section}.{sub}+{item}")
        # An MCP server grant is useless without a tool allowance.
        if gkind == "mcp":
            tools = sec.setdefault("tools", {})
            names = tools.setdefault(value, [])
            if "*" not in names:
                names.append("*")
                added.append(f"{section}.tools.{value}+*")
    before = "absent"
    after = ", ".join(added) if added else "already granted"
    return before, after
