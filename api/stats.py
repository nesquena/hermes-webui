"""
Hermes Web UI -- Stats aggregation API.

Read-only SQL aggregation over state.db for the Insights panel.
All queries use SELECT-only connections (file:...?mode=ro URI).
Each profile has an independent DB path resolved via api.profiles.

Endpoints served (registered in api/routes.py handle_get):
- GET /api/stats/summary
- GET /api/stats/timeseries
- GET /api/stats/response-time
- GET /api/stats/heatmap
- GET /api/stats/models

All responses are JSON; all queries go through a 30s in-memory TTL cache
keyed by (endpoint, query_string, profile_hermes_home). The ?refresh=1
query parameter bypasses the cache.

See openspec/changes/add-dashboards-and-pixel-office/design.md (D1, D6)
and specs/insights-panel/spec.md for the contract.

Implementation tasks in tasks.md sections 2.1-2.11.
"""
import logging

logger = logging.getLogger(__name__)


# ── Read-only DB connection ────────────────────────────────────────────────

def _open_state_db_readonly():
    """TODO 2.1: Open state.db with mode=ro URI for the active profile.

    Returns an sqlite3.Connection or None if state.db does not exist.
    """
    raise NotImplementedError("stats._open_state_db_readonly not implemented yet")


def _get_profile_db_path():
    """TODO 2.2: Resolve state.db path for the currently active profile.

    Delegates to api.profiles.get_active_hermes_home().
    """
    raise NotImplementedError("stats._get_profile_db_path not implemented yet")


# ── Cache ─────────────────────────────────────────────────────────────────

def _cached(ttl_seconds: int = 30):
    """TODO 2.3: Decorator producing a per-profile TTL cache.

    Cache key = (func_name, profile_hermes_home_path, frozen_kwargs).
    Honours `refresh=True` kwarg to bypass cache.
    """
    raise NotImplementedError("stats._cached not implemented yet")


# ── Handlers (bound in api/routes.py) ──────────────────────────────────────

def handle_stats_summary(handler, parsed):
    """TODO 2.4: GET /api/stats/summary.

    Response: {total_messages, total_input_tokens, total_output_tokens,
               total_cost_usd, active_webui_sessions, last_activity_ts}.
    Source: sessions SUM of token/cost columns + MAX(messages.timestamp)
    + webui SESSIONS LRU size.
    """
    from api.helpers import bad
    return bad(handler, "Not implemented", status=501)


def handle_stats_timeseries(handler, parsed):
    """TODO 2.5: GET /api/stats/timeseries?granularity=&window=&source=.

    source=total -> messages.token_count per DATE(timestamp).
    source=split -> sessions input/output/cache_read/reasoning per
    DATE(started_at).
    """
    from api.helpers import bad
    return bad(handler, "Not implemented", status=501)


def handle_stats_response_time(handler, parsed):
    """TODO 2.6: GET /api/stats/response-time?window=.

    Bucket user->assistant timestamp deltas (0-1s / 1-3s / 3-10s / 10-30s /
    30s+), filtering deltas > 600s as session pauses.
    """
    from api.helpers import bad
    return bad(handler, "Not implemented", status=501)


def handle_stats_heatmap(handler, parsed):
    """TODO 2.7: GET /api/stats/heatmap?window=.

    Returns a 7x24 weekday/hour matrix of message counts using
    strftime('%w'/%H, timestamp, 'unixepoch').
    """
    from api.helpers import bad
    return bad(handler, "Not implemented", status=501)


def handle_stats_models(handler, parsed):
    """TODO 2.8: GET /api/stats/models?window=.

    Returns [{model, input_tokens, output_tokens, message_count,
              estimated_cost_usd, pct}] sorted by total token desc.
    """
    from api.helpers import bad
    return bad(handler, "Not implemented", status=501)
