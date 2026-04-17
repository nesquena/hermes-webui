"""
Hermes Web UI -- Agent-activity and Surface aggregation API.

Produces per-surface activity snapshots and Surface Dashboard cards from
state.db + the webui-process SESSIONS LRU cache.

IMPORTANT: This module intentionally does NOT expose agent runtime state
(current_tool / pending_count / is_running_tool / ...). The webui process
cannot reliably observe such state through state.db or SESSIONS — those
fields live inside the hermes-agent process. Any future surface_runtime
signals require a dedicated agent<->webui IPC channel and belong in a
separate change. See design.md D9 for the rationale.

Endpoints served (registered in api/routes.py handle_get):
- GET /api/agent-activity
- GET /api/agent-activity/stream   (SSE)
- GET /api/surfaces                (snapshot + optional ?source=X&expand=1)

All handlers rely on GatewayWatcher.subscribe() for change notifications;
no additional polling thread is introduced.

See openspec/changes/add-dashboards-and-pixel-office/design.md (D3, D4, D9, D10)
and specs/agent-activity-api, surface-dashboard for the contracts.

Implementation tasks in tasks.md sections 3.1-3.10 and 4.1-4.5.
"""
import logging

logger = logging.getLogger(__name__)


# ── State derivation ───────────────────────────────────────────────────────

def derive_state(last_msg_ts, now_ts):
    """TODO 3.2: Map time-since-last-message to surface state.

    Rules (design D4, agent-activity-api spec "状态推导规则"):
      < 60s           -> 'working'
      60s  .. 300s    -> 'waiting'
      300s .. 24h     -> 'idle'
      > 24h / None    -> 'offline'

    Intentionally does NOT take a "has_active_session" argument — webui
    cannot reliably observe active sessions for non-webui surfaces.
    """
    raise NotImplementedError("agent_activity.derive_state not implemented yet")


# ── Snapshot builders (pure functions, easy to unit-test) ──────────────────

def build_surface_snapshot(db_path):
    """TODO 3.1: Build the /api/agent-activity snapshot.

    Performs SELECT DISTINCT source FROM sessions each call — surface
    enumeration is dynamic per active profile, not fixed at startup
    (design D3).

    Returns:
        {
          "surfaces": [
            {"source", "state", "last_active_ts",
             "message_count_24h", "tokens_24h",
             "active_webui_sessions"?  # only when source == "webui"
            },
            ...
          ],
          "generated_at": <unix-seconds>,
          "profile": <active-profile-name>,
        }
    """
    raise NotImplementedError("agent_activity.build_surface_snapshot not implemented yet")


def build_surfaces_cards(db_path):
    """TODO 4.1: Build the /api/surfaces card snapshot.

    Same shape as build_surface_snapshot but with 24h aggregates, and
    tailored for the Surfaces dashboard UI. See spec surface-dashboard
    'Surface 卡片内容' and '/api/surfaces 聚合端点'.
    """
    raise NotImplementedError("agent_activity.build_surfaces_cards not implemented yet")


def build_surface_expand(db_path, source):
    """TODO 4.2: Build {sessions: [...]} for ?source=X&expand=1.

    Returns the 5 most recent sessions for the requested source, with
    title redacted via _redact_text. Returns {sessions: []} for unknown
    source (not 404).
    """
    raise NotImplementedError("agent_activity.build_surface_expand not implemented yet")


# ── Handlers (bound in api/routes.py) ──────────────────────────────────────

def handle_agent_activity(handler, parsed):
    """TODO 3.4: GET /api/agent-activity."""
    from api.helpers import bad
    return bad(handler, "Not implemented", status=501)


def handle_agent_activity_stream(handler, parsed):
    """TODO 3.6: GET /api/agent-activity/stream (SSE).

    Emits:
      event: snapshot  -- full snapshot on connect
      event: delta     -- changed surfaces only, driven by GatewayWatcher events
      event: heartbeat -- every 30s of idleness to keep proxies alive
    Closes the connection when the active profile changes (task 3.7).
    """
    from api.helpers import bad
    return bad(handler, "Not implemented", status=501)


def handle_surfaces(handler, parsed):
    """TODO 4.2: GET /api/surfaces  and  GET /api/surfaces?source=X&expand=1."""
    from api.helpers import bad
    return bad(handler, "Not implemented", status=501)
