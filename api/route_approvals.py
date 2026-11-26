"""Approval SSE state and helpers.

State-extraction prelude to the routes.py split tracked in #1907.
Extracts approval state, not handlers, by design.

Gateway approvals carry two pieces of state with one lifecycle:

* the **producer** entry in ``tools.approval._gateway_queues`` — the queued
  approval itself, which owns the lifecycle; and
* the **mirror** entry in ``_pending`` — this module's projection of that
  producer for the UI.

A producer is consumed only by an explicit settlement operation, which assigns
its result and signals its waiter before fallible projection work begins.
``retire_gateway_pending_mirror`` cleans up the UI projection; it must not
silently delete a producer, because that would strand the agent thread parked
on the producer's event. For a provider-qualified response,
``resolve_gateway_pending_run`` matches the exact ``(run_id, approval_id)``
pair, settles that producer, and preserves same-run siblings and other runs.
When a run becomes terminal, ``settle_gateway_pending_run`` fail-closes every
producer owned by that run with ``deny`` before retiring its mirrors; producers
owned by another run remain live.
"""
import functools
import inspect
import logging
import queue
import threading
import uuid
from contextlib import contextmanager

from api.session_events import publish_session_list_changed

logger = logging.getLogger(__name__)

# ── Agent approval bridge (#9119) ────────────────────────────────────────────
# WebUI shares live state with the Agent's ``tools.approval``: its lock, its
# gateway queues, and the functions that resolve, grant and toggle YOLO. Most of
# these names are private to the Agent, so bind each one separately. A single
# ``from tools.approval import (...)`` swapped ALL of them for private stubs when
# ANY one was missing (the Agent removed ``_pending``/``submit_pending``), and
# approvals then "succeeded" in WebUI while the Agent thread timed out.
#
# * No Agent importable (CI, agent-less installs): local WebUI-only mode. Every
#   name below is a local stand-in; there is no Agent approval to bridge to.
# * Agent importable: each name binds to the Agent's own object. A missing name
#   is recorded in ``APPROVAL_BRIDGE`` and logged, and the routes that need it
#   fail closed instead of acting on a private stand-in.
try:
    import tools.approval as _agent_approval
except ImportError:
    _agent_approval = None


# The Agent's ``save_permanent_allowlist`` reads config.yaml before it takes
# ``_lock``, so two saves in one profile can overlap: the one that writes last
# merges a read taken before the other's write, and both grants can be lost.
# One lock held across each whole save closes that window. The module attribute
# is replaced because the Agent's own ``_persist_choice`` looks the name up when
# it runs, so its saves and WebUI's go through the same lock.
#
# Lock order: this lock first, then the Agent's ``_lock`` inside the save. Never
# call the save while holding ``_lock`` (the Agent releases it before saving,
# and every WebUI caller saves outside it).
def _serialize_allowlist_saves(module):
    """Wrap ``module.save_permanent_allowlist`` in one lock, once; return that lock."""
    save = getattr(module, "save_permanent_allowlist", None)
    if not callable(save):
        return None
    if getattr(save, "_webui_serialized", False):  # a reload or second import
        return save._webui_save_lock
    lock = threading.Lock()

    @functools.wraps(save)
    def serialized_save(*args, **kwargs):
        with lock:
            return save(*args, **kwargs)

    serialized_save._webui_serialized = True
    serialized_save._webui_original = save
    serialized_save._webui_save_lock = lock
    module.save_permanent_allowlist = serialized_save
    return lock


_allowlist_save_lock = _serialize_allowlist_saves(_agent_approval)


def _keys_persist_choice(module):
    """The Agent's ``_persist_choice(session_key, choice, keys)``, else None.

    Agents before 2026-10-06 (v2026.9.24) take ``(key, description, is_tirith)``
    tuples as the third argument; WebUI then grants and saves the keys itself.
    """
    persist = getattr(module, "_persist_choice", None)
    if not callable(persist):
        return None
    try:
        params = list(inspect.signature(persist).parameters)
    except Exception:
        return None
    return persist if len(params) >= 3 and params[2] == "keys" else None


# Read once. Not a bridge failure when None: WebUI uses its own grant calls.
persist_grant_choice = _keys_persist_choice(_agent_approval)

_MISSING = object()
# Without these WebUI cannot see or settle the Agent's parked approvals.
_BRIDGE_CRITICAL_NAMES = ("_lock", "_gateway_queues", "resolve_gateway_approval")
# "session" / "always" choices persist through these.
_SCOPE_GRANT_NAMES = (
    "approve_session",
    "approve_permanent",
    "save_permanent_allowlist",
    "_permanent_approved",
)
_YOLO_NAMES = ("enable_session_yolo", "disable_session_yolo", "is_session_yolo_enabled")
_missing_agent_names: list[str] = []


def _bind_agent_name(name: str, local_fallback):
    """Return the Agent's own ``tools.approval.<name>``, else ``local_fallback``."""
    if _agent_approval is None:
        return local_fallback
    value = getattr(_agent_approval, name, _MISSING)
    if value is _MISSING:
        _missing_agent_names.append(name)
        return local_fallback
    return value


# Current Agents use a re-entrant lock; older ones used a plain Lock, so WebUI
# must never take ``_lock`` while already holding it (no WebUI path does). The
# Agent-less stand-in is a plain Lock too, so a re-entry hangs in CI instead of
# only against an older Agent.
_lock = _bind_agent_name("_lock", threading.Lock())
_gateway_queues = _bind_agent_name("_gateway_queues", {})
resolve_gateway_approval = _bind_agent_name("resolve_gateway_approval", lambda *a, **k: 0)
approve_session = _bind_agent_name("approve_session", lambda *a, **k: None)
approve_permanent = _bind_agent_name("approve_permanent", lambda *a, **k: None)
save_permanent_allowlist = _bind_agent_name("save_permanent_allowlist", lambda *a, **k: None)
_permanent_approved = _bind_agent_name("_permanent_approved", set())
enable_session_yolo = _bind_agent_name("enable_session_yolo", lambda *a, **k: None)
disable_session_yolo = _bind_agent_name("disable_session_yolo", lambda *a, **k: None)
is_session_yolo_enabled = _bind_agent_name("is_session_yolo_enabled", lambda *a, **k: False)
# ``_pending`` is WebUI's projection of pending approvals. Older Agents also
# write raw no-notifier entries into their own ``_pending`` (and never read it),
# so share that dict while it exists; newer Agents dropped it and WebUI keeps
# the projection locally. Not a bridge failure either way.
_pending = getattr(_agent_approval, "_pending", None)
if not isinstance(_pending, dict):
    _pending = {}

# A prompt auto-answered the instant it arrived never entered the queue, yet the
# Gateway can describe the same prompt again through its ``pending_controls``
# projection. The event's consent markers are kept here for the very same
# prompt — same session, run, identity and generation — so that second
# description stays classifiable instead of being parked as unclassified.
# Nothing is inferred from a miss: an unknown prompt stays unclassified.
_answered_approval_markers: dict[tuple[str, str, str, int], tuple[list[str], str]] = {}
_ANSWERED_APPROVAL_MARKERS_MAX = 32

APPROVAL_BRIDGE = {
    "agent": _agent_approval is not None,
    "available": not any(n in _missing_agent_names for n in _BRIDGE_CRITICAL_NAMES),
    "scopes_available": not any(n in _missing_agent_names for n in _SCOPE_GRANT_NAMES),
    "yolo_available": not any(n in _missing_agent_names for n in _YOLO_NAMES),
    "missing": sorted(_missing_agent_names),
}
if _agent_approval is None:
    logger.info("tools.approval is not importable; approvals use local WebUI-only state")
elif _missing_agent_names:
    (logger.error if not APPROVAL_BRIDGE["available"] else logger.warning)(
        "Hermes Agent tools.approval (%s) lacks %s; WebUI approval bridge %s",
        getattr(_agent_approval, "__file__", "?"),
        ", ".join(APPROVAL_BRIDGE["missing"]),
        "is UNAVAILABLE: approval responses will fail closed"
        if not APPROVAL_BRIDGE["available"]
        else "is degraded: the affected approval features will refuse requests",
    )

_APPROVAL_BRIDGE_UNAVAILABLE_ERROR = (
    "The installed Hermes Agent lacks approval functions this WebUI needs ({missing}), "
    "so {feature} cannot reach the agent. Update WebUI and Hermes Agent together."
)


class ApprovalBridgeUnavailable(RuntimeError):
    """The Agent's approvals cannot be settled from WebUI (see APPROVAL_BRIDGE)."""


def require_approval_bridge() -> None:
    """Raise before WebUI shows an Agent approval it could never settle.

    The streaming notify callback calls this first: the Agent treats a raising
    notifier as ``notify_failed`` and denies at once, instead of parking the
    turn until ``approvals.timeout`` behind a card whose answer goes nowhere.
    """
    if not APPROVAL_BRIDGE["available"]:
        raise ApprovalBridgeUnavailable(
            "WebUI approval bridge unavailable; tools.approval lacks "
            + ", ".join(APPROVAL_BRIDGE["missing"])
        )


def approval_bridge_failure(*, choice: str = "", yolo: bool = False) -> tuple[dict, int] | None:
    """Return a fail-closed ``(payload, status)`` when the Agent cannot honour this, else None.

    ``choice`` is the approval answer about to be applied in-process; ``yolo`` is
    true for a YOLO toggle. Agent-less mode has no Agent to bridge to and never
    fails here.
    """
    if choice and not APPROVAL_BRIDGE["available"]:
        code, status, feature = "approval_bridge_unavailable", 503, "this approval response"
    elif choice in ("session", "always") and not APPROVAL_BRIDGE["scopes_available"]:
        code, status, feature = "approval_scope_unavailable", 409, f"an '{choice}' approval"
    elif yolo and not APPROVAL_BRIDGE["yolo_available"]:
        code, status, feature = "yolo_unavailable", 503, "session YOLO"
    else:
        return None
    payload = {
        "ok": False,
        "code": code,
        "error": _APPROVAL_BRIDGE_UNAVAILABLE_ERROR.format(
            missing=", ".join(APPROVAL_BRIDGE["missing"]), feature=feature
        ),
        "missing": list(APPROVAL_BRIDGE["missing"]),
    }
    if choice:
        payload["choice"] = choice
    return payload, status


# ── Approval SSE subscribers (long-connection push) ──────────────────────────
_approval_sse_subscribers: dict[str, list[queue.Queue]] = {}
_GATEWAY_MIRROR_FLAG = "_gateway_mirror"
_GATEWAY_MIRROR_TOKEN = "_gateway_mirror_token"
_GATEWAY_MIRROR_RETAINED = "_gateway_mirror_retained"
_GATEWAY_ENTRY_DATA_TOKEN_KEY = "_webui_mirror_token"
_GATEWAY_AGENT_IDENTITY_V1 = "_gateway_agent_identity_v1"
_GATEWAY_EXECUTION_GENERATION = "_gateway_execution_generation"
_gateway_relay_owners: dict[tuple[str, str], str] = {}
_yolo_transition_lock = threading.Lock()
_yolo_transitions: dict[str, dict] = {}
_gateway_yolo_handoff_guard = threading.Lock()
_gateway_yolo_handoffs: dict[str, dict] = {}

# ── Session YOLO revision (#9101) ────────────────────────────────────────────
# The flag lives in the Agent's memory. WebUI stamps every value it writes or
# observes with a number from one process-wide counter, and the GET/POST and
# approval answers carry it, so a POST can be made conditional on what its
# caller read (``expected_revision`` -> 409 ``yolo_changed`` when it moved).
# The epoch names this server process: a restart clears the flag and restarts
# the counter, so revisions from different epochs are never equal. Nothing here
# is persisted or pushed on a stream.
_YOLO_EPOCH = uuid.uuid4().hex[:16]
_yolo_revision_counter = 0
_yolo_revisions: dict[str, tuple[int, bool]] = {}  # session -> (revision, enabled)


def _yolo_state_locked(session_key: str) -> dict:
    """Current YOLO state of one session; a changed flag takes the next revision.

    Caller holds ``_yolo_transition_lock``. Writers call this right after
    writing, so revision order is write order.
    """
    global _yolo_revision_counter
    enabled = bool(is_session_yolo_enabled(session_key))
    entry = _yolo_revisions.get(session_key)
    moved = (entry[1] != enabled) if entry else enabled
    if moved:
        _yolo_revision_counter += 1
        entry = (_yolo_revision_counter, enabled)
        _yolo_revisions[session_key] = entry
    return {
        "yolo_enabled": enabled,
        "yolo_revision": entry[0] if entry else 0,
        "yolo_epoch": _YOLO_EPOCH,
    }


def session_yolo_state(session_key: str) -> dict:
    """``{yolo_enabled, yolo_revision, yolo_epoch}`` for one session."""
    session_key = str(session_key or "").strip()
    with _yolo_transition_lock:
        return _yolo_state_locked(session_key)


def yolo_revision_conflict(session_key: str, expected: tuple[int, str | None] | None) -> dict | None:
    """Refusal payload when the session's YOLO moved past ``expected``, else None."""
    if expected is None:
        return None
    state = session_yolo_state(session_key)
    revision, epoch = expected
    if state["yolo_revision"] == revision and epoch in (None, state["yolo_epoch"]):
        return None
    return {
        "ok": False,
        "code": "yolo_changed",
        "error": "YOLO changed since it was read; nothing was toggled.",
        **state,
    }


@contextmanager
def gateway_yolo_handoff(session_key: str):
    """Serialize one session's YOLO toggles with gateway approval dispatch."""
    session_key = str(session_key or "").strip()
    with _gateway_yolo_handoff_guard:
        entry = _gateway_yolo_handoffs.get(session_key)
        if entry is None:
            entry = {"lock": threading.Lock(), "users": 0}
            _gateway_yolo_handoffs[session_key] = entry
        entry["users"] += 1
    lock = entry["lock"]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _gateway_yolo_handoff_guard:
            entry["users"] -= 1
            if entry["users"] == 0:
                _gateway_yolo_handoffs.pop(session_key, None)


def begin_session_yolo_transition(session_key: str) -> object | None:
    """Register a pending YOLO enable until one approval relay settles.

    Multiple tabs may relay approvals for different runs in the same session.
    Track every in-flight enable intent so one failed relay cannot undo another
    successful or explicit enable. Do not publish an unconfirmed enable to the
    shared session flag: the gateway stream may only auto-approve later prompts
    after a relay succeeds or an explicit enable wins.
    """
    session_key = str(session_key or "").strip()
    if not session_key:
        return None
    token = object()
    with _yolo_transition_lock:
        transition = _yolo_transitions.get(session_key)
        if transition is None:
            transition = {
                "was_enabled": bool(is_session_yolo_enabled(session_key)),
                "tokens": set(),
                "committed": False,
            }
            _yolo_transitions[session_key] = transition
        transition["tokens"].add(token)
    return token


def finish_session_yolo_transition(session_key: str, token: object | None, *, succeeded: bool) -> None:
    """Settle one pending YOLO enable without exposing or applying stale state."""
    session_key = str(session_key or "").strip()
    if not session_key or token is None:
        return
    with _yolo_transition_lock:
        transition = _yolo_transitions.get(session_key)
        if transition is None or token not in transition["tokens"]:
            return
        transition["tokens"].remove(token)
        if succeeded:
            transition["committed"] = True
            # The first confirmed relay commits YOLO immediately. Any remaining
            # tokens may fail later but cannot revoke this successful enable.
            enable_session_yolo(session_key)
            _yolo_state_locked(session_key)
        if transition["tokens"]:
            return
        _yolo_transitions.pop(session_key, None)
        if transition["committed"] or transition["was_enabled"]:
            enable_session_yolo(session_key)
        else:
            disable_session_yolo(session_key)
        _yolo_state_locked(session_key)


def set_session_yolo_enabled(session_key: str, enabled: bool) -> None:
    """Apply an explicit YOLO choice and supersede in-flight rollbacks."""
    session_key = str(session_key or "").strip()
    if not session_key:
        return
    with _yolo_transition_lock:
        _yolo_transitions.pop(session_key, None)
        if enabled:
            enable_session_yolo(session_key)
        else:
            disable_session_yolo(session_key)
        _yolo_state_locked(session_key)


def _approval_sse_subscribe(session_id: str) -> queue.Queue:
    """Register an SSE subscriber for approval events on a given session."""
    q = queue.Queue(maxsize=16)
    with _lock:
        _approval_sse_subscribers.setdefault(session_id, []).append(q)
    return q


def _approval_sse_unsubscribe(session_id: str, q: queue.Queue) -> None:
    """Remove an SSE subscriber."""
    with _lock:
        subs = _approval_sse_subscribers.get(session_id)
        if subs and q in subs:
            subs.remove(q)
            if not subs:
                _approval_sse_subscribers.pop(session_id, None)


def _approval_sse_notify_locked(session_id: str, head: dict | None, total: int) -> None:
    """Push an approval event to all SSE subscribers for a session.

    CALLER MUST HOLD `_lock`. Snapshots the subscriber list under the held
    lock and then calls `q.put_nowait()` on each (which is itself thread-safe).

    `head` is the approval entry currently at the head of the queue (the one
    the UI should display) — NOT the just-appended entry. With multiple
    parallel approvals (#527), the just-appended entry is at the TAIL, but
    `/api/approval/pending` always returns the HEAD, so SSE must match.

    `total` is the total number of pending approvals.

    Pass `head=None` and `total=0` when the queue has just been emptied (e.g.
    `_handle_approval_respond` popped the last entry) so the client knows to
    hide its approval card.
    """
    payload = {"pending": dict(head) if head else None, "pending_count": total}
    subs = _approval_sse_subscribers.get(session_id, ())
    for q in subs:
        try:
            q.put_nowait(payload)
        except queue.Full:
            pass  # drop if subscriber is slow (bounded queue prevents memory leak)


def _approval_sse_notify(session_id: str, head: dict | None, total: int) -> None:
    """Convenience wrapper that takes `_lock` itself.

    Use only from contexts that don't already hold `_lock`. Production call
    sites (submit_pending, _handle_approval_respond) MUST hold the lock and
    call `_approval_sse_notify_locked` directly to avoid a notify-ordering
    race where a later append's notify can fire before an earlier append's
    notify (resulting in stale `pending_count`).
    """
    with _lock:
        _approval_sse_notify_locked(session_id, head, total)


def _gateway_mirror_entry_token(entry) -> str | None:
    """Return a stable token for the current process lifetime of a gateway head.

    Stamps a token key into the entry's `.data` dict so
    slotted objects like `_ApprovalEntry` work without attribute mutation
    and the token survives CPython `id()` reuse after GC.
    """
    data = getattr(entry, "data", None)
    if isinstance(data, dict):
        token = data.get(_GATEWAY_ENTRY_DATA_TOKEN_KEY)
        if not token:
            token = uuid.uuid4().hex
            data[_GATEWAY_ENTRY_DATA_TOKEN_KEY] = token
        return token
    return None


def _is_gateway_mirror_entry(entry: dict | None) -> bool:
    return isinstance(entry, dict) and bool(entry.get(_GATEWAY_MIRROR_FLAG))


#: Pattern keys the Agent stamps on a consent prompt. ``mcp_elicitation``
#: is the one the Agent's ``request_elicitation_consent`` writes for an MCP
#: trust/spend gate, a vault payment confirmation and a plugin's publish
#: consent. The others are the namespaced keys the Agent gives the consent
#: prompts that are not elicitations: a plugin's own approval rule (the
#: "ask before an outbound send / a public post / a money change" gate) and
#: the guard on writes to a protected instruction file.
_PROTECTED_PATTERN_KEYS = frozenset({
    "mcp_elicitation",
    "elicitation",
    "protected_instruction_file",
})

#: Prefixes of the namespaced consent pattern keys. A plugin rule is
#: ``plugin_rule:<rule_key>`` (the rule key is the plugin's own), and a
#: computer-use action is ``cua:<action>:<mode>`` — both ask a person to
#: confirm an action the agent would otherwise take on its own.
_PROTECTED_PATTERN_KEY_PREFIXES = ("plugin_rule:", "cua:")

#: Approval surfaces that carry a consent prompt rather than a command. The
#: Agent names the surface of a consent ask ``mcp-elicitation``,
#: ``mcp-trust/<server>`` (a per-chat MCP spend limit) or ``vault-payment``
#: (filling a saved card); ``publish`` is the consent surface the Agent's
#: publish-style plugins use. A surface prefix match covers the
#: ``mcp-trust/<server>`` form, where the server name is part of the value.
_PROTECTED_SURFACES = frozenset({"publish", "mcp-elicitation", "vault-payment"})
_PROTECTED_SURFACE_PREFIXES = ("mcp-elicitation", "mcp-trust/", "vault-payment")


def _is_protected_pattern_key(key: str) -> bool:
    """True for a pattern key the Agent uses for a consent prompt."""
    if not key:
        return False
    if key in _PROTECTED_PATTERN_KEYS:
        return True
    return key.startswith(_PROTECTED_PATTERN_KEY_PREFIXES)


def _is_protected_surface(surface: str) -> bool:
    """True for a consent surface name (exact match or a known prefix)."""
    if not surface:
        return False
    if surface in _PROTECTED_SURFACES:
        return True
    return surface.startswith(_PROTECTED_SURFACE_PREFIXES)


def is_protected_approval(approval) -> bool:
    """True for a consent prompt that session YOLO must never answer (#9108).

    An MCP elicitation (``pattern_key: "mcp_elicitation"``, also carried in
    ``pattern_keys``), a plugin's own approval rule
    (``plugin_rule:<rule_key>``) and any approval raised on a consent surface
    are consent prompts for an outbound send, a public post, a money change or
    a destructive action. YOLO is a statement about *command* approvals only,
    so both settle paths and the YOLO enable/release path must leave these to
    an explicit answer.

    The consent marker matters because a Gateway may describe the very same
    consent prompt by its surface instead of its pattern key, and a Gateway
    that projects a pending prompt without the pattern key (the
    ``pending_controls`` authority projection carries no ``pattern_key`` at
    all) would otherwise leave the prompt unprotectable.
    """
    if not isinstance(approval, dict):
        return False
    keys = [str(approval.get("pattern_key") or "")] + [
        str(k) for k in (approval.get("pattern_keys") or [])
    ]
    if any(_is_protected_pattern_key(key) for key in keys):
        return True
    return _is_protected_surface(str(approval.get("surface") or "").strip())


def is_classified_approval(approval) -> bool:
    """True when the payload states WHICH kind of gate this prompt is.

    A consent prompt and an ordinary command approval are told apart by the
    Agent's own marker (``pattern_key`` / ``pattern_keys``) or by the consent
    surface. A payload carrying none of them cannot be classified at all:
    the canonical Gateway producer rebuilds a pending approval from kind,
    prompt id, generation, command, description and choices, and its
    ``pending_controls`` projection has no marker either (#9108).

    Missing classification is NOT evidence that the prompt is an ordinary
    command, so it must not buy an automatic answer; callers park the prompt
    as a card instead.
    """
    if not isinstance(approval, dict):
        return False
    keys = [str(approval.get("pattern_key") or "")] + [
        str(k) for k in (approval.get("pattern_keys") or [])
    ]
    if any(key.strip() for key in keys):
        return True
    return bool(str(approval.get("surface") or "").strip())


def may_auto_answer_approval(approval) -> bool:
    """True only for a CLASSIFIED, non-consent approval.

    Session YOLO may answer this one automatically. Everything else is left to
    an explicit answer: a consent prompt because YOLO never covers consent, and
    an unclassified prompt because nothing in the payload proves it is not one.
    """
    return is_classified_approval(approval) and not is_protected_approval(approval)


def remember_answered_approval_markers(session_key: str, approval: dict) -> None:
    """Remember the consent markers of a prompt WebUI just settled.

    The Gateway describes one prompt twice: an ``approval.request`` event that
    carries the Agent's marker, and a ``pending_controls`` projection that may
    carry none. When the event is answered before the projection arrives, the
    queue entry is gone and the projection would look unclassified, so the
    markers are remembered against the prompt itself — same session, run,
    identity and execution generation — and dropped oldest-first once the
    bound is reached.
    """
    if not isinstance(approval, dict):
        return
    prompt_id = str(
        approval.get("prompt_id") or approval.get("approval_id") or ""
    ).strip()
    run_id = str(approval.get("run_id") or "").strip()
    generation = approval.get(_GATEWAY_EXECUTION_GENERATION)
    if not prompt_id or not run_id or type(generation) is not int:
        return
    keys = [str(approval.get("pattern_key") or "")] + [
        str(k) for k in (approval.get("pattern_keys") or [])
    ]
    keys = [key.strip() for key in keys if key.strip()]
    surface = str(approval.get("surface") or "").strip()
    if not keys and not surface:
        return
    with _lock:
        _answered_approval_markers[(session_key, run_id, prompt_id, generation)] = (
            keys,
            surface,
        )
        while len(_answered_approval_markers) > _ANSWERED_APPROVAL_MARKERS_MAX:
            _answered_approval_markers.pop(next(iter(_answered_approval_markers)))


def known_approval_consent_markers(
    session_key: str,
    *identifiers: str,
    execution_generation: int | None = None,
    run_id: str = "",
) -> tuple[list[str], str]:
    """The consent markers WebUI already knows for one parked approval.

    A Gateway may re-describe a prompt that WebUI has already seen: the
    ``approval.request`` event carries the Agent's ``pattern_key``, while the
    ``pending_controls`` authority projection carries the same prompt without
    one. When the projection arrives for a prompt whose event WebUI already
    mirrored, the consent markers are still on that mirror, and dropping them
    would let session YOLO answer a consent prompt that the event path
    protects.

    A prompt id alone is not enough to call it the same prompt: the Gateway
    may re-publish one id at a newer execution generation, and the newer
    generation can describe a DIFFERENT command. Copying the older
    generation's consent markers onto it would park an ordinary command for a
    manual answer. The markers are therefore recovered only for the same
    run AND the same execution generation, and never for a prompt whose
    generation is unknown on either side.

    Returns ``(pattern_keys, surface)`` — the pattern keys and the surface
    this session already recorded for a prompt matching any of *identifiers*
    (the approval id, the prompt id or the request id). Both are empty when
    nothing matches, so the caller keeps the payload's own values.
    """
    wanted = {str(identifier or "").strip() for identifier in identifiers}
    wanted.discard("")
    if not wanted:
        return [], ""
    wanted_run_id = str(run_id or "").strip()
    pattern_keys: list[str] = []
    surface = ""
    with _lock:
        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            ids = {
                str(entry.get("approval_id") or "").strip(),
                str(entry.get("prompt_id") or "").strip(),
                str(entry.get("request_id") or "").strip(),
            }
            if not (ids & wanted):
                continue
            # The same prompt id may describe a different command at a newer
            # execution generation, so the markers are only recoverable for the
            # very same prompt: same run, same generation, and a generation
            # this side knows on both.
            if wanted_run_id:
                entry_run_id = str(entry.get("run_id") or "").strip()
                if entry_run_id and entry_run_id != wanted_run_id:
                    continue
            if type(execution_generation) is int:
                entry_generation = entry.get(_GATEWAY_EXECUTION_GENERATION)
                if type(entry_generation) is not int:
                    continue
                if entry_generation != execution_generation:
                    continue
            else:
                # An unknown generation cannot be proven to be the same
                # prompt, and a newer generation may be a different command.
                if entry.get(_GATEWAY_EXECUTION_GENERATION) is not None:
                    continue
            for key in entry.get("pattern_keys") or []:
                key = str(key or "").strip()
                if key and key not in pattern_keys:
                    pattern_keys.append(key)
            if not pattern_keys:
                single = str(entry.get("pattern_key") or "").strip()
                if single:
                    pattern_keys.append(single)
            surface = surface or str(entry.get("surface") or "").strip()
        if (
            not pattern_keys
            and not surface
            and wanted_run_id
            and type(execution_generation) is int
        ):
            # The prompt may have been settled before this projection arrived;
            # the markers then live in the answered-prompt table, not the queue.
            for identifier in wanted:
                cached = _answered_approval_markers.get(
                    (session_key, wanted_run_id, identifier, execution_generation)
                )
                if cached is not None:
                    pattern_keys, surface = list(cached[0]), cached[1]
                    break
    return pattern_keys, surface


def _normalize_pending_queue_locked(session_key: str) -> list[dict]:
    """Return the session's polling queue as a mutable list under `_lock`."""
    queue_list = _pending.setdefault(session_key, [])
    if not isinstance(queue_list, list):
        _pending[session_key] = [queue_list]
        queue_list = _pending[session_key]
    return queue_list


def reconcile_gateway_pending_mirror_locked(session_key: str) -> tuple[dict | None, int, bool]:
    """Purge stale gateway mirrors and ensure at most one live head mirror exists.

    CALLER MUST HOLD `_lock`.
    """
    changed = False
    queue_list = list(_normalize_pending_queue_locked(session_key))
    live_gateway_queue = _gateway_queues.get(session_key) or []

    live_head_entry = live_gateway_queue[0] if live_gateway_queue else None
    live_head_data = getattr(live_head_entry, "data", None) or {}
    live_run_id = str(live_head_data.get("run_id") or "").strip()
    # Tokenize EVERY live no-run producer, and derive `live_token` from the
    # authoritative head, whenever `_gateway_queues[session_key]` has live
    # producers. This deliberately does NOT defer to a pre-existing no-run
    # mirror: an unmatched/tokenless mirror A (which the fail-closed binding in
    # submit_gateway_pending_mirror leaves deliberately unbound) must never
    # suppress the live producer's token, or A masks the real pending approval
    # B — B never surfaces as the head and can't be actioned, while responding
    # to A resolves nothing. While any producer is live, a mirror survives only
    # if it is bound to some live producer's own token — the head's via
    # `live_token`, a non-head producer's via `live_local_tokens` (which is what
    # keeps a non-head mirror resolvable) — so unmatched and tokenless copies
    # are discarded instead of masking a real one. Tokenless-orphan retention is
    # reserved for the genuine no-producer case (#7093), which lands here with an
    # empty `live_gateway_queue` and therefore a `None` `live_token` anyway.
    live_local_tokens: set[str] = set()
    for live_entry in live_gateway_queue:
        live_data = getattr(live_entry, "data", None) or {}
        if str(live_data.get("run_id") or "").strip():
            continue
        live_entry_token = _gateway_mirror_entry_token(live_entry) or ""
        if live_entry_token:
            live_local_tokens.add(live_entry_token)
            if not str(live_data.get("approval_id") or "").strip():
                live_data["approval_id"] = f"gwlocal:{live_entry_token}"

    # A raw id-less LOCAL entry in `_pending` (agent-side _pending_result drops
    # `{command, pattern_key, pattern_keys, description}` verbatim when no
    # gateway notifier is registered) is unactionable by contract: the frontend
    # owner-capture requires an approval_id, so its card can neither be
    # approved nor dismissed — the poll re-serves it forever. Mint a stable id
    # on the entry itself (the same contract the producer-tokenization loop
    # above applies to _gateway_queues producers). Minting on the stored dict
    # keeps the id stable across polls, so frontend dismiss markers persist.
    for entry in queue_list:
        if not isinstance(entry, dict) or _is_gateway_mirror_entry(entry):
            continue
        if str(entry.get("approval_id") or "").strip():
            continue
        if str(entry.get("run_id") or "").strip():
            continue
        entry["approval_id"] = f"gwlocal-mirrorless:{uuid.uuid4().hex}"
    live_token = (
        _gateway_mirror_entry_token(live_head_entry)
        if live_head_entry and live_head_data
        else None
    )
    if live_token and live_run_id and not str(live_head_data.get("approval_id") or "").strip():
        live_head_data["approval_id"] = f"gwrun:{live_run_id}:{live_token}"
    live_approval_id = str(live_head_data.get("approval_id") or "").strip()

    rebuilt: list[dict] = []
    deferred_run_entries: list[dict] = []
    live_mirror_present = False
    for entry in queue_list:
        if not _is_gateway_mirror_entry(entry):
            rebuilt.append(entry)
            continue
        entry_run_id = str(entry.get("run_id") or "").strip()
        entry_approval_id = str(entry.get("approval_id") or "").strip()
        entry_token = str(entry.get(_GATEWAY_MIRROR_TOKEN) or "").strip()
        matches_live_head = False
        if live_token:
            if entry_token and entry_token == live_token:
                matches_live_head = True
            elif (
                live_approval_id
                and live_run_id
                and entry_approval_id == live_approval_id
                and entry_run_id == live_run_id
            ):
                matches_live_head = True

        if entry_run_id:
            if matches_live_head and not live_mirror_present:
                if entry_token != live_token:
                    entry[_GATEWAY_MIRROR_TOKEN] = live_token
                    changed = True
                rebuilt.append(entry)
                live_mirror_present = True
                continue
            if live_token:
                if entry.get(_GATEWAY_MIRROR_RETAINED):
                    rebuilt.append(entry)
                    continue
                if entry_token:
                    changed = True
                    continue
                deferred_run_entries.append(entry)
                continue
            if entry.get(_GATEWAY_MIRROR_RETAINED) or not entry_token:
                rebuilt.append(entry)
                continue
            changed = True
            continue

        # A retained mirror is one whose own producer had already vanished when
        # the user responded (the missing-producer 409 kept visible until an
        # explicit teardown). It survives ONLY while no producer is live: once
        # `_gateway_queues[session_key]` holds a real producer again, an
        # unresolvable retained mirror must not mask it, so fall through to the
        # normal matching below (which keeps it if it still matches a live
        # token and discards it otherwise).
        if entry.get(_GATEWAY_MIRROR_RETAINED) and not live_gateway_queue:
            rebuilt.append(entry)
            continue

        if matches_live_head and not live_mirror_present:
            if entry_token != live_token:
                entry[_GATEWAY_MIRROR_TOKEN] = live_token
                changed = True
            rebuilt.append(entry)
            live_mirror_present = True
            continue

        if entry_token and entry_token in live_local_tokens:
            rebuilt.append(entry)
            continue

        if not live_token:
            if entry_token:
                changed = True
                continue
            rebuilt.append(entry)
            continue

        changed = True

    if live_token and not live_mirror_present:
        mirror_entry = dict(live_head_data)
        mirror_run_id = str(mirror_entry.get("run_id") or "").strip()
        mirror_entry.setdefault(
            "approval_id",
            f"gwrun:{mirror_run_id}:{live_token}" if mirror_run_id else uuid.uuid4().hex,
        )
        mirror_entry[_GATEWAY_MIRROR_FLAG] = True
        mirror_entry[_GATEWAY_MIRROR_TOKEN] = live_token
        rebuilt.append(mirror_entry)
        live_mirror_present = True
        changed = True

    if deferred_run_entries:
        rebuilt.extend(deferred_run_entries)

    if rebuilt:
        if rebuilt != queue_list:
            _pending[session_key] = rebuilt
            changed = True
    else:
        if session_key in _pending:
            _pending.pop(session_key, None)
            changed = True

    head = rebuilt[0] if rebuilt else None
    total = len(rebuilt)
    return head, total, changed


def _gateway_pending_mirror_locked(
    session_key: str,
    approval_id: str = "",
    run_id: str = "",
    mirror_token: str = "",
) -> dict | None:
    """Return the exact live run-backed mirror under `_lock`."""
    approval_id = str(approval_id or "").strip()
    run_id = str(run_id or "").strip()
    mirror_token = str(mirror_token or "").strip()
    queue = _pending.get(session_key)
    entries = queue if isinstance(queue, list) else [queue] if queue else []
    if approval_id:
        matched_entry: dict | None = None
        for entry in entries:
            if not _is_gateway_mirror_entry(entry):
                continue
            if entry.get("approval_id") != approval_id:
                continue
            entry_run_id = str(entry.get("run_id") or "").strip()
            if not entry_run_id:
                if not run_id:
                    return None
                continue
            if run_id and entry_run_id != run_id:
                continue
            if mirror_token and str(entry.get(_GATEWAY_MIRROR_TOKEN) or "").strip() != mirror_token:
                continue
            if run_id:
                return entry
            if matched_entry is not None:
                return None
            matched_entry = entry
        return matched_entry
    for entry in entries:
        if not _is_gateway_mirror_entry(entry) or not str(entry.get("run_id") or "").strip():
            continue
        if mirror_token and str(entry.get(_GATEWAY_MIRROR_TOKEN) or "").strip() != mirror_token:
            continue
        if run_id:
            if entry.get("run_id") == run_id:
                return entry
            continue
        # With no caller-supplied identity, the queue order is authoritative:
        # return the current run-backed projection and let its embedded
        # `(approval_id, run_id)` identify the exact relay owner.
        return entry
    return None


def gateway_pending_mirror(
    session_key: str,
    approval_id: str = "",
    run_id: str = "",
    mirror_token: str = "",
) -> dict | None:
    """Return an exact live run-backed mirror for this session."""
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_key)
        entry = _gateway_pending_mirror_locked(session_key, approval_id, run_id, mirror_token)
        return dict(entry) if entry else None


def gateway_pending_mirrors(session_key: str) -> list[dict]:
    """Return every currently parked run-backed mirror in queue order."""
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_key)
        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        return [
            dict(entry)
            for entry in entries
            if _is_gateway_mirror_entry(entry)
            and str(entry.get("run_id") or "").strip()
        ]


def claim_gateway_approval_relay_owner(session_key: str, run_id: str, approval_id: str) -> bool:
    """Claim the single-flight relay owner for one `(session, run)` pair."""
    session_key = str(session_key or "").strip()
    run_id = str(run_id or "").strip()
    approval_id = str(approval_id or "").strip()
    if not session_key or not run_id:
        return False
    with _lock:
        key = (session_key, run_id)
        if key in _gateway_relay_owners:
            return False
        _gateway_relay_owners[key] = approval_id
        return True


def release_gateway_approval_relay_owner(session_key: str, run_id: str, approval_id: str = "") -> None:
    """Release the single-flight relay owner for one `(session, run)` pair."""
    session_key = str(session_key or "").strip()
    run_id = str(run_id or "").strip()
    approval_id = str(approval_id or "").strip()
    if not session_key or not run_id:
        return
    with _lock:
        key = (session_key, run_id)
        current = str(_gateway_relay_owners.get(key) or "").strip()
        if approval_id and current and current != approval_id:
            return
        _gateway_relay_owners.pop(key, None)


def _settle_gateway_entry(entry, choice: str, reason: str | None = None) -> None:
    """Complete a removed producer before any fallible projection work."""
    entry.result = choice
    if reason:
        entry.reason = reason
    entry.event.set()


def retire_gateway_pending_mirror(
    session_key: str,
    approval_id: str = "",
    run_id: str = "",
    mirror_token: str = "",
) -> bool:
    """Retire one approval mirror, or every mirror for a terminal run."""
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_key)
        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        normalized_run_id = str(run_id or "").strip()
        if approval_id:
            match = _gateway_pending_mirror_locked(
                session_key,
                approval_id,
                run_id,
                mirror_token,
            )
            if match is None and not normalized_run_id:
                match = next((entry for entry in entries if _is_gateway_mirror_entry(entry)
                              and not str(entry.get("run_id") or "").strip()
                              and str(entry.get("approval_id") or "").strip() == approval_id), None)
            retired = [match] if match else []
        else:
            retired = [
                entry for entry in entries
                if _is_gateway_mirror_entry(entry)
                and str(entry.get("run_id") or "").strip() == normalized_run_id
            ] if normalized_run_id else [
                entry for entry in entries
                if _is_gateway_mirror_entry(entry)
                and not str(entry.get("run_id") or "").strip()
            ]
        if not retired:
            head, total, changed = reconcile_gateway_pending_mirror_locked(session_key)
            _approval_sse_notify_locked(session_key, head, total)
            if changed:
                publish_session_list_changed("attention_resolved")
            return changed
        for match in retired:
            entries.remove(match)
        if entries:
            _pending[session_key] = entries
        else:
            _pending.pop(session_key, None)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    publish_session_list_changed("attention_resolved")
    return True


def settle_gateway_pending_run(
    session_key: str,
    run_id: str,
    *,
    reason: str,
) -> tuple[int, dict | None, int]:
    """Fail-close every producer and mirror owned by a terminal gateway run."""
    run_id = str(run_id or "").strip()
    if not run_id:
        return 0, None, 0

    targets = []
    with _lock:
        gateway_queue = _gateway_queues.get(session_key) or []
        retained_gateway_queue = []
        for entry in gateway_queue:
            data = getattr(entry, "data", None) or {}
            if str(data.get("run_id") or "").strip() == run_id:
                targets.append(entry)
            else:
                retained_gateway_queue.append(entry)
        if retained_gateway_queue:
            _gateway_queues[session_key] = retained_gateway_queue
        else:
            _gateway_queues.pop(session_key, None)

        # Removal and waiter settlement are one semantic operation. In
        # particular, reconciliation and SSE notification below may raise.
        for entry in targets:
            _settle_gateway_entry(entry, "deny", reason)

        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        retained_pending = [
            entry
            for entry in entries
            if not (
                _is_gateway_mirror_entry(entry)
                and str(entry.get("run_id") or "").strip() == run_id
            )
        ]
        mirrors_retired = len(retained_pending) != len(entries)
        if retained_pending:
            _pending[session_key] = retained_pending
        else:
            _pending.pop(session_key, None)

        head, total, changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    if targets or mirrors_retired or changed:
        publish_session_list_changed("attention_resolved")
    return len(targets), head, total


def _gateway_mirrored_pending_run_id(session_key: str, approval_id: str) -> str | None:
    """Compatibility wrapper for exact run-backed lookup."""
    approval_id = str(approval_id or "").strip()
    if not approval_id:
        return None
    with _lock:
        entry = _gateway_pending_mirror_locked(session_key, approval_id=approval_id)
        if entry:
            return str(entry.get("run_id") or "").strip() or None
    return None


def submit_gateway_pending_mirror(session_key: str, approval: dict, *, cancel_event=None) -> tuple[dict | None, int]:
    """Mirror the live gateway head into WebUI polling state under a typed tag.

    Every mirrored entry describes one pending approval to the UI. Run-backed
    mirrors carry ``run_id`` (remote gateway runs) and are bound to the parked
    ``_ApprovalEntry`` via ``approval_id``. No-run mirrors, which represent an
    in-process (legacy) approval parked in ``_gateway_queues``, have no
    ``run_id`` and instead bind back to the live entry through
    ``_GATEWAY_MIRROR_TOKEN``: ``_resolve_approval_legacy()`` matches
    ``pending[_GATEWAY_MIRROR_TOKEN]`` against the live entry's
    ``_webui_mirror_token`` before it will call ``resolve_gateway_pending_local()``
    to unblock the agent thread. A no-run mirror's token is stamped from its
    OWN live producer — resolved via the mirror's ``request_id``/
    ``approval_id`` — and from nothing else. If no live producer's identity
    matches, the mirror is left tokenless (fail closed): guessing ownership
    from "first unclaimed token" or the queue head would let approving THIS
    (possibly stale/foreign) mirror resolve a DIFFERENT live producer than
    the one the user actually saw, which is an approval-integrity violation,
    not a convenience. A tokenless mirror is only wired up automatically when
    there is truly no live producer at all (#7093); ``reconcile_gateway_
    pending_mirror_locked`` binds a fresh, correctly-bound mirror to the
    authoritative live head on its own. Without a token, THIS specific click
    returns ``ok:true`` and the card clears without unblocking any producer —
    the correct producer's own card reappears on the next reconcile.
    """
    with _lock:
        if cancel_event is not None and cancel_event.is_set():
            return None, 0
        run_id = str(approval.get("run_id") or "").strip()
        approval_id = str(approval.get("approval_id") or "").strip()
        live_gateway_queue = _gateway_queues.get(session_key) or []
        exact_local_entry = next(
            (
                entry for entry in live_gateway_queue
                if getattr(entry, "data", None) is approval
            ),
            None,
        ) if not run_id else None
        if exact_local_entry is None and not run_id and approval_id:
            exact_local_entry = next(
                (
                    entry for entry in live_gateway_queue
                    if str(((getattr(entry, "data", None) or {}).get("approval_id") or "")).strip() == approval_id
                ),
                None,
            )
        if exact_local_entry is None and not run_id:
            # Fall back to matching on the core's per-approval `request_id`.
            # The gateway core notifies WebUI with a COPY of the entry payload
            # (`notify_cb(dict(entry.data))`), so the identity match above
            # (`entry.data is approval`) never holds for a real gateway head,
            # and a local `_ApprovalEntry` carries a `request_id` but no
            # `approval_id`, so the approval_id fallback misses too. The
            # `request_id` is stamped once on the source entry
            # (`_ApprovalEntry.__init__` -> `data.setdefault("request_id", ...)`)
            # and preserved through the copy, so it uniquely reunites the
            # notified copy with its queued entry. Without this, the mirror is
            # created with no token, reconcile keeps the orphan, and
            # `_session_has_pending_approval` stays True after the entry is
            # dropped (the stale-approval-card dead-end, #4948 local variant).
            request_id = str(approval.get("request_id") or "").strip()
            if request_id:
                exact_local_entry = next(
                    (
                        entry for entry in live_gateway_queue
                        if str(((getattr(entry, "data", None) or {}).get("request_id") or "")).strip() == request_id
                    ),
                    None,
                )
        if exact_local_entry is not None:
            mirror_entries = _normalize_pending_queue_locked(session_key)
            entries_to_mirror = [live_gateway_queue[0]] if live_gateway_queue else []
            if exact_local_entry not in entries_to_mirror:
                entries_to_mirror.append(exact_local_entry)
            for entry in entries_to_mirror:
                local_data = entry.data
                token = _gateway_mirror_entry_token(entry)
                entry_approval_id = str(local_data.get("approval_id") or "").strip()
                if entry is exact_local_entry:
                    entry_approval_id = approval_id or entry_approval_id or f"gwlocal:{token}"
                    approval_id = entry_approval_id
                    approval["approval_id"] = entry_approval_id
                elif not entry_approval_id:
                    entry_approval_id = f"gwlocal:{token}"
                local_data["approval_id"] = entry_approval_id
                if not any(
                    _is_gateway_mirror_entry(mirror)
                    and str(mirror.get(_GATEWAY_MIRROR_TOKEN) or "") == token
                    for mirror in mirror_entries
                ):
                    mirror_entry = dict(local_data)
                    mirror_entry["approval_id"] = entry_approval_id
                    mirror_entry[_GATEWAY_MIRROR_FLAG] = True
                    mirror_entry[_GATEWAY_MIRROR_TOKEN] = token
                    mirror_entries.append(mirror_entry)
        if run_id:
            live_head_entry = live_gateway_queue[0] if live_gateway_queue else None
            live_head_data = getattr(live_head_entry, "data", None) or {}
            live_head_run_id = str(live_head_data.get("run_id") or "").strip()
            live_head_approval_id = str(live_head_data.get("approval_id") or "").strip()
            live_token = (
                _gateway_mirror_entry_token(live_head_entry)
                if live_head_entry and live_head_data
                else None
            )
            if (
                live_token
                and live_head_run_id == run_id
                and (
                    not approval_id
                    or not live_head_approval_id
                    or live_head_approval_id == approval_id
                )
            ):
                if approval_id:
                    live_head_data["approval_id"] = approval_id
                else:
                    approval_id = live_head_approval_id
                    if not approval_id:
                        approval_id = f"gwrun:{run_id}:{live_token}"
                        live_head_data["approval_id"] = approval_id
                    approval["approval_id"] = approval_id
            else:
                if not approval_id:
                    approval_id = f"gwrun:{run_id}:{uuid.uuid4().hex}"
                    approval["approval_id"] = approval_id
                mirror_entry = dict(approval)
                mirror_entry["run_id"] = run_id
                mirror_entry["approval_id"] = approval_id
                mirror_entry[_GATEWAY_MIRROR_FLAG] = True
                mirror_entry[_GATEWAY_MIRROR_TOKEN] = uuid.uuid4().hex
                mirror_entry[_GATEWAY_MIRROR_RETAINED] = True
                existing_mirror = _gateway_pending_mirror_locked(
                    session_key, approval_id=approval_id, run_id=run_id
                )
                if existing_mirror is None:
                    _normalize_pending_queue_locked(session_key).append(mirror_entry)
                elif existing_mirror.get(_GATEWAY_EXECUTION_GENERATION) != mirror_entry.get(
                    _GATEWAY_EXECUTION_GENERATION
                ):
                    # The run re-published this prompt id at a new generation:
                    # the old mirror's answer is a stale 409, so the live
                    # payload replaces it (new payload, new mirror token)
                    # instead of being deduped away by the id alone.
                    queue = _normalize_pending_queue_locked(session_key)
                    queue[:] = [entry for entry in queue if entry is not existing_mirror]
                    queue.append(mirror_entry)
        elif not exact_local_entry:
            if not approval_id:
                approval_id = uuid.uuid4().hex
                approval["approval_id"] = approval_id
            queue = _pending.get(session_key)
            entries = queue if isinstance(queue, list) else [queue] if queue else []
            no_run_mirror = next(
                (
                    entry for entry in reversed(entries)
                    if _is_gateway_mirror_entry(entry)
                    and not str(entry.get("run_id") or "").strip()
                    and str(entry.get("approval_id") or "").strip() == approval_id
                ),
                None,
            )
            if no_run_mirror:
                approval["approval_id"] = str(no_run_mirror.get("approval_id") or approval_id).strip()
            elif not _gateway_pending_mirror_locked(session_key, approval_id=approval_id):
                # Stamp the mirror token from the mirror's OWN live producer so
                # the first respond can link this mirror to the right
                # _ApprovalEntry in _gateway_queues. Without it,
                # _resolve_approval_legacy cannot match the no-run mirror to its
                # gateway entry (both token fields are empty) and the agent
                # thread is never unblocked on the first click (#6008 legacy).
                # We MUST NOT blindly take the live head: for a non-head mirror
                # (multiple parked producers, #7093 exact-producer isolation)
                # the head belongs to a sibling, and stamping its token would
                # bind the mirror to the wrong entry. Only an explicit
                # request_id/approval_id match may bind a token.
                #
                # FAIL CLOSED when no producer's identity matches: never infer
                # ownership from "first unclaimed token" or the queue head. A
                # stale/foreign approval (mismatched request_id, or no
                # identity at all) that borrows another live producer's token
                # would let approving THIS mirror resolve a DIFFERENT producer
                # than the one the user actually saw — an approval-integrity
                # violation, not a convenience (found in review of 818fd2fd).
                # A tokenless orphan is only legitimate when there is no live
                # producer at all (#7093); reconcile_gateway_pending_mirror_locked
                # binds a real mirror to the authoritative live head on its own.
                live_queue_for_mirror = _gateway_queues.get(session_key) or []
                request_id_for_mirror = str(approval.get("request_id") or "").strip()
                mirror_producer = None
                if request_id_for_mirror or approval_id:
                    for cand in live_queue_for_mirror:
                        cand_data = getattr(cand, "data", None) or {}
                        if (request_id_for_mirror and
                                str(cand_data.get("request_id") or "").strip() == request_id_for_mirror):
                            mirror_producer = cand
                            break
                        if (approval_id and not request_id_for_mirror and
                                str(cand_data.get("approval_id") or "").strip() == approval_id):
                            mirror_producer = cand
                            break
                mirror_token = (
                    _gateway_mirror_entry_token(mirror_producer)
                    if mirror_producer is not None else None
                )
                mirror_entry = dict(approval)
                mirror_entry["approval_id"] = approval_id
                mirror_entry[_GATEWAY_MIRROR_FLAG] = True
                if mirror_token:
                    mirror_entry[_GATEWAY_MIRROR_TOKEN] = mirror_token
                _normalize_pending_queue_locked(session_key).append(mirror_entry)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    publish_session_list_changed("attention_pending")
    return (dict(head) if head else None), total


def resolve_gateway_pending_local(
    session_key: str, approval_id: str, choice: str, reason: str | None = None
) -> tuple[int, dict | None, int]:
    """Resolve the exact parked local entry bound to an approval mirror."""
    target = None
    with _lock:
        approval_id = str(approval_id or "").strip()
        gateway_queue = _gateway_queues.get(session_key) or []
        for index, entry in enumerate(gateway_queue):
            data = getattr(entry, "data", None) or {}
            if str(data.get("approval_id") or "").strip() == approval_id:
                target = gateway_queue.pop(index)
                break
        if gateway_queue:
            _gateway_queues[session_key] = gateway_queue
        else:
            _gateway_queues.pop(session_key, None)
        if target is not None:
            _settle_gateway_entry(target, choice, reason)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    if target is None:
        return 0, head, total
    publish_session_list_changed("attention_resolved")
    return 1, head, total


def resolve_gateway_pending_run(
    session_key: str,
    approval_id: str,
    run_id: str,
    choice: str,
    reason: str | None = None,
) -> tuple[int, dict | None, int]:
    """Resolve one exact run-backed producer and wake its parked waiter."""
    target = None
    approval_id = str(approval_id or "").strip()
    run_id = str(run_id or "").strip()
    if not approval_id or not run_id:
        return 0, None, 0
    with _lock:
        gateway_queue = _gateway_queues.get(session_key) or []
        retained_gateway_queue = []
        for entry in gateway_queue:
            data = getattr(entry, "data", None) or {}
            entry_approval_id = str(data.get("approval_id") or "").strip()
            entry_run_id = str(data.get("run_id") or "").strip()
            if target is None and entry_approval_id == approval_id and entry_run_id == run_id:
                target = entry
            else:
                retained_gateway_queue.append(entry)
        if retained_gateway_queue:
            _gateway_queues[session_key] = retained_gateway_queue
        else:
            _gateway_queues.pop(session_key, None)
        if target is not None:
            _settle_gateway_entry(target, choice, reason)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    if target is None:
        return 0, head, total
    publish_session_list_changed("attention_resolved")
    return 1, head, total


def resolve_gateway_pending_local_no_run_mirror(
    session_key: str, approval_id: str, choice: str, reason: str | None = None
) -> tuple[bool, int, dict | None, int]:
    """Resolve an exact no-run mirror only while its parked producer still exists."""
    target = None
    with _lock:
        approval_id = str(approval_id or "").strip()
        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        matched_mirror = next(
            (
                entry for entry in entries
                if _is_gateway_mirror_entry(entry)
                and not str(entry.get("run_id") or "").strip()
                and str(entry.get("approval_id") or "").strip() == approval_id
            ),
            None,
        )
        if matched_mirror is None:
            return False, 0, entries[0] if entries else None, len(entries)

        gateway_queue = _gateway_queues.get(session_key) or []
        for index, entry in enumerate(gateway_queue):
            data = getattr(entry, "data", None) or {}
            if str(data.get("approval_id") or "").strip() == approval_id:
                target = gateway_queue.pop(index)
                break
        if target is None:
            matched_mirror[_GATEWAY_MIRROR_RETAINED] = True
            return True, 0, entries[0] if entries else None, len(entries)

        if gateway_queue:
            _gateway_queues[session_key] = gateway_queue
        else:
            _gateway_queues.pop(session_key, None)
        entries.remove(matched_mirror)
        if entries:
            _pending[session_key] = entries
        else:
            _pending.pop(session_key, None)
        _settle_gateway_entry(target, choice, reason)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)
    publish_session_list_changed("attention_resolved")
    return True, 1, head, total


def resolve_gateway_pending_local_all(
    session_key: str,
    choice: str,
    reason: str | None = None,
) -> tuple[int, dict | None, int]:
    """Resolve every parked local/no-run approval without touching remote runs.

    Protected consent prompts (elicitation / publish, ``is_protected_approval``)
    are retained unpublished-for-answer instead of drained: the YOLO auto-answer
    behind this drain must not sweep one up, whichever ordinary command
    triggered the drain (#9108).
    """
    targets = []
    removed_pending = False
    with _lock:
        reconcile_gateway_pending_mirror_locked(session_key)

        gateway_queue = _gateway_queues.get(session_key) or []
        retained_gateway_queue = []
        for entry in gateway_queue:
            data = getattr(entry, "data", None) or {}
            if str(data.get("run_id") or "").strip() or is_protected_approval(data):
                retained_gateway_queue.append(entry)
            else:
                targets.append(entry)
        if retained_gateway_queue:
            _gateway_queues[session_key] = retained_gateway_queue
        else:
            _gateway_queues.pop(session_key, None)

        queue = _pending.get(session_key)
        entries = queue if isinstance(queue, list) else [queue] if queue else []
        retained_pending = [
            entry
            for entry in entries
            if (
                _is_gateway_mirror_entry(entry)
                and str(entry.get("run_id") or "").strip()
            )
            or is_protected_approval(entry)
        ]
        removed_pending = len(retained_pending) != len(entries)
        if retained_pending:
            _pending[session_key] = retained_pending
        else:
            _pending.pop(session_key, None)

        for entry in targets:
            _settle_gateway_entry(entry, choice, reason)
        head, total, _changed = reconcile_gateway_pending_mirror_locked(session_key)
        _approval_sse_notify_locked(session_key, head, total)

    if targets or removed_pending:
        publish_session_list_changed("attention_resolved")
    return len(targets), head, total


def settle_gateway_pending_local_notification(
    session_key: str,
    approval: dict,
) -> tuple[bool, dict | None, int]:
    """Auto-resolve or publish one local approval at the YOLO handoff boundary.

    The Agent adds its blocking entry before invoking WebUI's notify callback.
    Serialize that callback with session YOLO commit/disable so a waiter arriving
    after a drain snapshot cannot be parked behind an already-committed enable.
    Run-backed approvals stay on the Runs API path and are never resolved here.
    """
    with gateway_yolo_handoff(session_key):
        run_id = str((approval or {}).get("run_id") or "").strip()
        if not run_id and not is_protected_approval(approval) and is_session_yolo_enabled(session_key):
            _resolved, head, total = resolve_gateway_pending_local_all(
                session_key,
                "once",
            )
            return True, head, total
        head, total = submit_gateway_pending_mirror(session_key, approval)
        return False, head, total


def submit_pending(session_key: str, approval: dict) -> None:
    """Append a pending approval to the per-session queue.

    Wraps the agent's submit_pending to:
    - Add a stable approval_id (uuid4 hex) so the respond endpoint can target
      a specific entry even when multiple approvals are queued simultaneously.
    - Change the storage from a single overwriting dict value to a list, so
      parallel tool calls each get their own approval slot (fixes #527).
    - Notify any connected SSE subscribers immediately.
    """
    entry = dict(approval)
    entry.setdefault("approval_id", uuid.uuid4().hex)
    with _lock:
        queue_list = _normalize_pending_queue_locked(session_key)
        queue_list.append(entry)
        total = len(queue_list)
        head = queue_list[0]  # /api/approval/pending always returns head
        # Push to SSE subscribers from inside _lock so two parallel
        # submit_pending calls can't deliver out-of-order (T2's later
        # notify arriving before T1's earlier notify with a stale count).
        _approval_sse_notify_locked(session_key, head, total)
    publish_session_list_changed("attention_pending")
    # NOTE: We do NOT call _submit_pending_raw here — that function overwrites
    # _pending[session_key] with a single dict, which would undo the list we just
    # built. The gateway blocking path uses _gateway_queues (a separate mechanism
    # managed by check_all_command_guards / register_gateway_notify), which is
    # unaffected by _pending. The _pending dict is only used for UI polling.
