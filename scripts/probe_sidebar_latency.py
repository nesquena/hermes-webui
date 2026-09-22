#!/usr/bin/env python3
"""Read-only standalone probe for the CLI-metadata lookup / sidebar latency.

Measures, against the ACTIVE profile's state.db (same resolution the WebUI
server uses):

  0. the interactive pass cold/warm
     (``read_importable_agent_session_rows(limit=20, exclude_sources=("cron",
     "webhook", "kanban"))`` — the sidebar's visible CLI/agent window, and the
     Slice D candidate-ordering target). Measured FIRST in the process so the
     first sample is the process's first state.db DATA read (cold); later
     samples are warm. The probe's own ``PRAGMA index_list(messages)`` guard and
     the pass's schema PRAGMAs run before it.
  1. the Claude Code JSONL scan alone (``get_claude_code_sessions()``),
  2. the OLD lookup cost: full ``get_cli_sessions()`` projection + a linear
     scan for the sid (exactly what routes.py did before Slice A),
  3. the NEW targeted lookup: ``models.lookup_cli_session_metadata(sid)``.

With ``--fast-sidebar-only`` (Slice C C4) the probe skips 0–3 and instead drives
the real ``_get_cached_session_list_payload`` with both builders through the
four cache states — post-restart/no-entry (cold: the fast first-paint payload is
served while the full rebuild runs in the background), warm (cache hit),
volatile-stale (served stale + background rebuild) and structural-stale (the
non-fast-gated synchronous full rebuild) — plus per-piece timings for the fast
build, the full build, ``all_sessions`` and the response conversion. Run it in
a FRESH process for the post-restart number; clear-cache alone is not a restart
(the models-layer CLI cache and the page cache survive).

SAFETY: this script never writes to the store. It opens state.db read-only
(URI mode=ro), and it monkeypatches every helper that could otherwise create
state: ``ensure_cron_project`` / ``ensure_webhook_project`` (projects.json),
``_profile_has_user_projects``, and ``get_last_workspace`` (workspace probes).
If ``idx_messages_session`` is missing from the target db, the projection's
defensive index self-heal would open a writable connection — the probe checks
for that up front and refuses to run unless ``--allow-index-selfheal`` is
passed (the live dbs all have the index). ``--fast-sidebar-only`` runs the real
sidebar builders, whose ``all_sessions`` can write the WebUI session index on a
backfill/recovery edge: point ``HERMES_HOME``/``HERMES_WEBUI_STATE_DIR`` at a
copy of the store for that mode unless a read-only live run is acceptable.

Usage (from the worktree root):
    python scripts/probe_sidebar_latency.py [--sid SID] [--runs 3]
    HERMES_HOME=/tmp/copy python scripts/probe_sidebar_latency.py --fast-sidebar-only --runs 5
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import statistics
import sys
import time
from contextlib import closing

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _pick_newest_interactive_sid(db_path: pathlib.Path) -> str | None:
    """Newest non-background sid via one indexed read (not part of the timing)."""
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    with closing(conn):
        cur = conn.cursor()
        for order in (
            "COALESCE(s.last_activity_at, s.started_at) DESC, s.started_at DESC",
            "s.started_at DESC",
        ):
            try:
                cur.execute(
                    "SELECT s.id FROM sessions s WHERE s.source IS NOT NULL"
                    " AND s.source NOT IN ('cron','webhook','kanban')"
                    f" ORDER BY {order} LIMIT 1"
                )
                row = cur.fetchone()
                if row:
                    return str(row[0])
            except sqlite3.Error:
                continue
    return None


def _messages_index_present(db_path: pathlib.Path) -> bool:
    try:
        conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    with closing(conn):
        try:
            cur = conn.cursor()
            cur.execute("PRAGMA index_list(messages)")
            return any(str(row[1]) == "idx_messages_session" for row in cur.fetchall())
        except sqlite3.Error:
            return False


def _timed(fn, runs: int) -> tuple[list[float], object]:
    samples: list[float] = []
    result = None
    for _ in range(runs):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples, result


def _fmt(samples: list[float]) -> str:
    return (
        f"median {statistics.median(samples):8.1f} ms | "
        f"min {min(samples):8.1f} | max {max(samples):8.1f} | "
        f"n={len(samples)}"
    )


def _percentile(samples: list[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * len(ordered))) - 1))
    return ordered[idx]


def _fast_sidebar_section(runs: int) -> int:
    """Slice C C4: drive the real sidebar cache through the four cache states.

    Uses the REAL builders (the fast first-paint builder + the full builder) and
    a controlled source stamp so every state is reachable deterministically:
    the probe monkeypatches ``routes._session_list_cache_source_stamp`` with a
    mutable cell, stores the payload under one stamp, then flips the volatile or
    structural part to produce the stale states. Runs the default /api/sessions
    shape (visible_only, sidebar_source='webui', exclude_hidden).
    """
    import api.models as models
    import api.routes as routes

    args = dict(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        show_claude_code_sessions=True,
        include_archived=False,
        exclude_hidden=True,
        visible_only=True,
        show_webhook_sessions=False,
        show_kanban_sessions=False,
        source_filter=None,
        sidebar_source="webui",
        archived_limit=None,
        archived_offset=0,
    )
    key = routes._session_list_cache_key(
        active_profile="default",
        all_profiles=False,
        show_cli_sessions=True,
        show_previous_messaging_sessions=False,
        show_cron_sessions=False,
        include_archived=False,
        exclude_hidden=True,
        visible_only=True,
        show_webhook_sessions=False,
        show_kanban_sessions=False,
        source_filter=None,
        sidebar_source="webui",
        archived_limit=None,
        archived_offset=0,
    )

    def fast_builder():
        return routes._build_session_list_fast_payload(**args)

    def full_builder():
        return routes._build_session_list_cache_payload(**args)

    stamp_cell = {"value": ("probe-structural-0", "probe-volatile-0")}
    routes._session_list_cache_source_stamp = lambda _key: stamp_cell["value"]

    def _wait_for_background_rebuild(timeout: float = 30.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with routes._SESSIONS_CACHE_LOCK:
                inflight = routes._SESSIONS_CACHE_INFLIGHT.get(key)
            if inflight is None:
                return True
            time.sleep(0.01)
        return False

    states: dict[str, list[float]] = {
        "post-restart/no-entry (fast)": [],
        "warm (cache hit)": [],
        "volatile-stale (served stale + bg rebuild)": [],
        "structural-stale (sync full rebuild)": [],
    }
    payload_stats: dict[str, object] = {}

    for iteration in range(max(1, runs)):
        # ── cold: no cache entry at all ──────────────────────────────────
        routes._session_list_cache_clear()
        stamp_cell["value"] = ("probe-structural-0", "probe-volatile-0")
        started = time.perf_counter()
        payload = routes._get_cached_session_list_payload(
            key=key, builder=full_builder, fast_builder=fast_builder,
        )
        states["post-restart/no-entry (fast)"].append((time.perf_counter() - started) * 1000.0)
        if iteration == 0:
            payload_stats = {
                "sessions": len(payload.get("sessions", [])),
                "cli_count": payload.get("cli_count"),
                "webui_session_count": payload.get("webui_session_count"),
                "cli_session_count": payload.get("cli_session_count"),
                "archived_count": payload.get("archived_count"),
                "other_profile_count": payload.get("other_profile_count"),
                "default_hidden": sum(
                    1 for row in payload.get("sessions", []) if row.get("default_hidden")
                ),
            }
        _wait_for_background_rebuild()

        # ── warm: the stored full payload is fresh under the same stamp ──
        started = time.perf_counter()
        routes._get_cached_session_list_payload(
            key=key, builder=full_builder, fast_builder=fast_builder,
        )
        states["warm (cache hit)"].append((time.perf_counter() - started) * 1000.0)

        # ── volatile-stale: message-write churn only ─────────────────────
        stamp_cell["value"] = ("probe-structural-0", f"probe-volatile-{iteration + 1}")
        started = time.perf_counter()
        routes._get_cached_session_list_payload(
            key=key, builder=full_builder, fast_builder=fast_builder,
        )
        states["volatile-stale (served stale + bg rebuild)"].append(
            (time.perf_counter() - started) * 1000.0
        )
        _wait_for_background_rebuild()

        # ── structural-stale: not fast-gated, rebuilds synchronously ─────
        stamp_cell["value"] = (f"probe-structural-{iteration + 1}", f"probe-volatile-{iteration + 1}")
        started = time.perf_counter()
        routes._get_cached_session_list_payload(
            key=key, builder=full_builder, fast_builder=fast_builder,
        )
        states["structural-stale (sync full rebuild)"].append(
            (time.perf_counter() - started) * 1000.0
        )

    routes._session_list_cache_clear()

    print("fast-sidebar cache states (default /api/sessions shape):")
    for state, samples in states.items():
        print(f"  {state:46s} p50 {statistics.median(samples):8.1f} ms | p90 {_percentile(samples, 90):8.1f} ms | {_fmt(samples)}")
    print(f"  payload: {payload_stats}")
    print()

    # ── per-piece timings (fresh samples; run AFTER the state loop so the
    # page cache reflects the state loop, i.e. a warm-ish process) ────────
    fast_samples, fast_payload = _timed(fast_builder, max(3, runs))
    print(f"  fast payload build alone      : {_fmt(fast_samples)}")
    full_samples, _ = _timed(full_builder, max(3, runs))
    print(f"  full payload build alone      : {_fmt(full_samples)}")
    all_samples, webui_rows = _timed(
        lambda: models.all_sessions(diag=None, include_lineage_metadata=False), max(3, runs)
    )
    print(f"  all_sessions (webui side)     : {_fmt(all_samples)}  rows={len(webui_rows or [])}")
    response_samples, _ = _timed(
        lambda: routes._session_list_payload_to_response(fast_payload), max(3, runs)
    )
    print(f"  response conversion (fast)    : {_fmt(response_samples)}")
    print()

    # ── parity spot check: fast vs full visible ids on real data ─────────
    routes._session_list_cache_clear()
    fast_payload = fast_builder()
    full_payload = full_builder()
    fast_ids = [row.get("session_id") for row in fast_payload.get("sessions", [])]
    full_ids = [row.get("session_id") for row in full_payload.get("sessions", [])]
    print(f"  parity (real data): fast={len(fast_ids)} rows full={len(full_ids)} rows "
          f"same_set={set(fast_ids) == set(full_ids)} same_order={fast_ids == full_ids}")
    for field in ("cli_count", "webui_session_count", "cli_session_count",
                  "archived_count", "archived_webui_count", "archived_cli_count",
                  "other_profile_count"):
        if fast_payload.get(field) != full_payload.get(field):
            note = ""
            if field in ("cli_count", "cli_session_count"):
                note = (" [BUG if nonzero: the fast payload runs the same Claude Code JSONL "
                        "scan as the full builder for the same shape, so these counts must "
                        "agree]")
            print(f"  parity count divergence {field}: fast={fast_payload.get(field)} "
                  f"full={full_payload.get(field)}{note}")
    diffs = 0
    full_by_id = {row.get("session_id"): row for row in full_payload.get("sessions", [])}
    for row in fast_payload.get("sessions", []):
        ref = full_by_id.get(row.get("session_id"))
        if ref is None:
            continue
        for field in ("title", "updated_at", "last_message_at", "message_count",
                      "actual_message_count", "is_cli_session", "source_tag", "raw_source",
                      "session_source", "source_label", "project_id", "pinned",
                      "archived", "relationship_type", "parent_session_id",
                      "_lineage_root_id", "_lineage_tip_id"):
            if row.get(field) != ref.get(field):
                diffs += 1
                if diffs <= 10:
                    print(f"  parity field diff {row.get('session_id')}.{field}: "
                          f"fast={row.get(field)!r} full={ref.get(field)!r}")
    print(f"  parity field diffs (client-read fields): {diffs}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sid", default=None, help="session id to look up")
    parser.add_argument("--runs", type=int, default=3, help="samples per measurement")
    parser.add_argument(
        "--allow-index-selfheal",
        action="store_true",
        help="run even if idx_messages_session is missing (the projection may then "
        "open a writable connection to self-heal it)",
    )
    parser.add_argument(
        "--fast-sidebar-only",
        action="store_true",
        help="Slice C C4: skip the CLI/lookup sections and measure the fast "
        "first-paint sidebar path through the four cache states (run in a fresh "
        "process for the post-restart number; point HERMES_HOME at a copy if the "
        "store must not be touched)",
    )
    args = parser.parse_args()

    import api.models as models

    # Read-only safety: never let the probe mint projects or touch workspace state.
    models.ensure_cron_project = lambda **_: None
    models.ensure_webhook_project = lambda: None
    models._profile_has_user_projects = lambda: False
    models.get_last_workspace = lambda: pathlib.Path("/tmp/probe-workspace")

    hermes_home, db_path, cli_profile, _cache_key = models._resolve_cli_sessions_context(None)
    print(f"hermes_home : {hermes_home}")
    print(f"state.db    : {db_path}  (profile={cli_profile or 'default'})")

    if not pathlib.Path(db_path).exists():
        print("state.db missing — nothing to measure.")
        return 2

    if not _messages_index_present(pathlib.Path(db_path)) and not args.allow_index_selfheal:
        print(
            "refusing to run: idx_messages_session is missing, so the projection "
            "would self-heal with a writable connection. Re-run with "
            "--allow-index-selfheal to accept that (never on the live store)."
        )
        return 3

    runs = max(1, args.runs)

    if args.fast_sidebar_only:
        print()
        return _fast_sidebar_section(runs)

    # 0) Interactive pass (the sidebar's visible CLI/agent window). Measured
    # FIRST in the process so sample 1 is the process's first state.db DATA read
    # (cold); later samples are warm. (The probe's own PRAGMA guard and the
    # pass's schema PRAGMAs have already touched the file.) The pass orders its
    # candidate window by the exact per-row MAX(messages.timestamp) subquery —
    # the same key the display sorts by, resolved per session through
    # ``idx_messages_session``. (A Slice-D interim ordered it by the indexed
    # ``COALESCE(s.last_activity_at, s.started_at)`` column; that column lags
    # the exact key by an unbounded amount, so an oversample of it is headroom,
    # not a membership bound — see the review fix on this PR.)
    def interactive_pass():
        return models.read_importable_agent_session_rows(
            db_path,
            limit=models.CLI_VISIBLE_SESSION_LIMIT,
            exclude_sources=("cron", "webhook", "kanban"),
        )

    cold_ms = None
    warm_samples: list[float] = []
    interactive_rows = 0
    for sample_index in range(runs):
        start = time.perf_counter()
        rows = interactive_pass()
        elapsed = (time.perf_counter() - start) * 1000.0
        interactive_rows = len(rows or [])
        if sample_index == 0:
            cold_ms = elapsed
        else:
            warm_samples.append(elapsed)
    warm_text = (
        f"median {statistics.median(warm_samples):8.1f} ms | "
        f"min {min(warm_samples):8.1f} | max {max(warm_samples):8.1f} | n={len(warm_samples)}"
        if warm_samples
        else "n/a (runs=1)"
    )
    print(
        f"interactive pass (limit={models.CLI_VISIBLE_SESSION_LIMIT}): "
        f"cold {cold_ms:8.1f} ms | warm {warm_text}  rows={interactive_rows}"
    )
    print()

    sid = args.sid or _pick_newest_interactive_sid(pathlib.Path(db_path))
    if not sid:
        print("no candidate session id found — pass --sid.")
        return 2
    print(f"target sid  : {sid}")
    print()

    cc_samples, cc_rows = _timed(lambda: models.get_claude_code_sessions(), runs)
    print(f"claude_code JSONL scan      : {_fmt(cc_samples)}  rows={len(cc_rows or [])}")

    def old_lookup():
        models.clear_cli_sessions_cache()  # cold, like the pre-Slice-A per-lookup rebuild
        for row in models.get_cli_sessions(all_profiles=False):
            if row.get("session_id") == sid:
                return row
        return {}

    before_samples, before_row = _timed(old_lookup, runs)
    print(f"OLD lookup (cold projection): {_fmt(before_samples)}  hit={bool(before_row)}")

    def warm_old_lookup():
        return models.get_cli_sessions(all_profiles=False)

    warm_samples, _ = _timed(warm_old_lookup, runs)
    print(f"OLD bulk projection (warm)  : {_fmt(warm_samples)}")

    after_samples, after_row = _timed(
        lambda: models.lookup_cli_session_metadata(sid), runs
    )
    print(f"NEW targeted lookup         : {_fmt(after_samples)}  hit={bool(after_row)}")
    print()

    if before_row and after_row and before_row != after_row:
        print("WARNING: lookup row differs from the bulk row for this sid:")
        keys = sorted(set(before_row) | set(after_row))
        for key in keys:
            if before_row.get(key) != after_row.get(key):
                print(f"  {key}: bulk={before_row.get(key)!r} lookup={after_row.get(key)!r}")
    elif not before_row and after_row:
        print(
            "note: the bulk window did NOT contain this sid (capped at "
            f"{models.CLI_VISIBLE_SESSION_LIMIT} rows); the targeted lookup resolves it "
            "— fidelity improvement over the old linear scan."
        )
    elif before_row and not after_row:
        print("WARNING: bulk contained this sid but the targeted lookup missed it.")
    else:
        print("equivalence: lookup row == bulk row (or both empty).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
