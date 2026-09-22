# Session-list fast first paint (`/api/sessions`)

This document records the current runtime contract for the sidebar session-list
payload: the fast first-paint builder, the full builder's ownership, the
fast/full parity contract, and the fallback behaviors. It describes shipped
behavior and changes no runtime behavior.

## Payload lifecycle

- **Route cache** (`api/route_session_list_cache.py`, wrapped by
  `api/routes._get_cached_session_list_payload`): keyed by the request shape —
  profile scope, `all_profiles`, the `show_*` flags, `include_archived`,
  `exclude_hidden`, `visible_only`, `source_filter`, `sidebar_source`,
  `archived_limit`/`archived_offset`, `show_claude_code_sessions`. TTL 2.5 s
  (45 s while a turn is streaming), max 64 entries.
- **Cache stamp** (`(structural, volatile)`): structural = sessions
  `MAX(rowid)` + session `_index.json` stat + settings stat/version. A
  structural change rebuilds synchronously on the request thread (`"source"`).
  volatile = messages `MAX(rowid)` + state.db/WAL stats + gateway metadata
  stat: the stale payload is served while a background rebuild runs (`"age"`),
  and the entry is never evicted for it.
- **Fast first paint** (`api/routes._build_session_list_fast_payload`): served
  only on a **cold** cache miss (no entry) and only for the default sidebar
  shape (`_session_list_fast_shape_eligible`: `visible_only`, single profile, no
  archive view, no archive paging, no background source filter). It is built
  from bounded indexed reads — webui rows through `all_sessions` with
  `state_db_override_counts=False` (tier-1 primary-key overlay only; no
  `messages` scan on the request thread), CLI/agent rows through
  `read_fast_sidebar_agent_rows` (the bounded candidate window below) plus the
  bounded cron/webhook/kanban chip passes (200 each), and the same Claude Code
  JSONL scan the full builder runs (bounded at `CLAUDE_CODE_MAX_FILES`,
  per-file parse cache) whenever the request shape enables those rows.
  **The fast payload is never stored** in any cache.
- **Full builder** (`_build_session_list_cache_payload`): the only writer of the
  route cache, and (through the non-`fast_window` loader) the only writer of the
  models-layer `get_cli_sessions` cache. On a cold miss the fast payload is
  served while the full payload rebuilds on a daemon thread; the background
  store keeps the stamp the payload was **built** from.
- **Owner/follower**: one request claims the rebuild (owner); concurrent
  requests (followers) serve the fast payload (cold) or the stale payload
  (volatile) without waiting for the owner.

## Fast/full parity contract

- Same payload contract and the same merge→sort→cap tail. Fast visible rows
  equal full visible rows for the same args: ids, order, title, `updated_at`,
  `message_count`, source flags, `project_id`, `pinned`, `archived`,
  `relationship_type`, `parent_session_id`, plus every payload count field
  (`tests/test_session_list_fast_path.py`).
- The fast window keeps the exact per-candidate `COUNT(m.id)` /
  `MAX(m.timestamp)` and the exact `COALESCE(MAX(mx.timestamp), s.started_at)`
  ordering key; only the user-turn aggregation is deferred — rows the
  visibility filter drops get one id-bounded `COUNT` follow-up
  (`_fill_fast_visibility_user_counts`) that reproduces the full projection's
  decision for default-titled CLI rows and ACP rows.
- Documented fast-only divergence: the webui tier-2 `last_message_at` overlay
  (a `messages` aggregation) waits for the background rebuild; the fast paint
  carries the sidecar value until then.

## Candidate window (fast reader)

- **Ordering key.** The candidate CTE orders by the exact
  `COALESCE(MAX(messages.timestamp), started_at)` key — the same key the display
  sorts by — never by the lagging `sessions.last_activity_at` (upstream #2662: a
  session resumed after a long gap ranks at the top by its latest message while
  the denormalized key ranks it past the window). The served slice is the
  display order's prefix **over the seeded candidate set**; the candidate set
  itself is a bounded seed set, not a provable prefix of the order over all
  sessions (see the bound below).
- **Why a union.** SQLite evaluates that ordering key for every qualifying row
  **before** `LIMIT`, so a window ordered directly by it costs one indexed
  message probe per qualifying session at any window size (measured warm medians
  / cold first run: 0.7 ms / 11 ms at 1k sessions, 9.9 ms / 102 ms at 10k,
  54.9 ms / 556 ms at 50k, 5.3 ms / 271 ms on a clone of the live ~3k-session
  4.2 GB store). The fast reader therefore seeds the candidate set with a
  bounded UNION of index-ordered pre-windows (`_fast_candidate_union_cte`) and
  applies the exact key only over that union. The probe count is bounded by the
  pre-window depth at any store size (measured 0.7-1.6 ms warm and 1-98 ms
  first-in-fresh-process across the same stores; page-cache dependent), and the
  final exact sort/membership is unchanged.
- **What the seeds are, precisely.** With `N` the visible limit, `W = 8N`
  (`FAST_SIDEBAR_CANDIDATE_OVERSAMPLE`) the candidate window and `M = 8W = 64N`
  (`FAST_SIDEBAR_PREWINDOW_OVERSAMPLE`) the message window, a session is in the
  candidate union iff it is (a) among the `W` sessions with the largest
  `COALESCE(last_activity_at, started_at)` (needs the column and
  `idx_sessions_effective_activity`), (b) among the `W` with the largest
  `started_at` (needs `idx_sessions_started`), (c) among the `W` with the
  largest `rowid`, or (d) the session of one of the newest `M` message rows.
  The window over the union equals the exact window over all qualifying sessions
  **iff** every session in that exact window satisfies (a)-(d).
- **Residual bound (reachable, not a theorem).** A session whose only recency
  evidence is its messages is seeded by (d) alone, so it is missed when its
  newest message is more than `M` message rows behind the tail while it fails
  (a)-(c) — NULL/stale `last_activity_at`, old `started_at`, low rowid. The
  reachable shape: a busy period dominated by a few long sessions that append
  more than `M` rows after that session's last message. Pinned counterexample
  (`tests/test_session_candidate_ordering_perf.py::test_candidate_union_bound_drops_a_row_beyond_the_message_window`):
  at `N=20` (`W=160`, `M=1280`) a target at exact rank 20 whose newest message
  sits behind 1530 newer rows is not seeded, so the union's top-20 replaces it
  with a filler row — the fast first paint omits a row the full reader (and the
  pre-union fast reader) returns, until the background full rebuild lands. A
  transient fast/full divergence, not data loss.
- **Measured headroom on the live store** (3017 sessions / 505k messages,
  read-only clone): the union's exact top-160 equals the exact top-160 over all
  qualifying sessions (limits 100 and 200 likewise). The message seed covers
  only 15 distinct sessions and 145 of the exact top-160 have their newest
  message more than 1280 message rows behind the tail (median 13,488, max
  26,259) — the three session-row seeds carry the coverage, not the message
  seed. The worst exact-top-160 row sits at rank 156 of 160 in its best
  session-row seed: 4 rows of slack at the boundary.
- **No provable bound check on this schema.** Bounding an excluded session's
  exact key needs either the denormalized column (unsound: it lags by
  construction, and the resumed/NULL shapes are exactly where it lags) or a
  global timestamp-ordered read of `messages` (there is no `messages(timestamp)`
  index, so it is a full scan of the 505k-row table — measured warm on a
  read-only clone: ~80-145 ms for a bare `MAX(timestamp)`, ~26-31 ms for
  `ORDER BY timestamp DESC LIMIT 1`, ~0.4-2.2 s for the rowid-filtered form,
  page-cache dependent — and rowid order is not timestamp order there: 403 rows
  inside the newest 5,000 have a newer-timestamped row before them, worst
  3,702 s (97,545 rows overall, worst ~9.9 d; counts are of the clone snapshot
  and drift as the live store grows, the inversion structure does not). A
  competitor bound built from the pre-window boundaries fires on every live
  request while the union is in fact exact (measured on the clone at limits
  20/100/200: the window's last candidate key sits days below the
  message-boundary timestamp that dominates the bound, so the bound check fires
  at every limit), i.e. it would always pay the exact-over-all window this path
  exists to avoid. The counterexample above is therefore documented and pinned
  rather than "proved away".
- The session-row seeds require the agent's standard indexes
  (`idx_sessions_effective_activity` / `idx_sessions_started`). Without them the
  plain exact-key window runs — the pre-union behavior, bound with its own
  parameters (`where_sql` + `LIMIT ?`) — instead of sorting the whole sessions
  table per pre-window. A fast CLI read that fails then propagates to the
  route's fast-build failure path (see Fallbacks); it is never degraded to an
  empty CLI list.
- The full reader keeps the exact-key window over all qualifying sessions: it
  runs in the background rebuild, not on the first-paint path, and it is the
  parity reference the fast window is checked against.

## Fallbacks

- Fast-build failure → the unchanged synchronous full build for that request
  (the fast payload is built before the rebuild event is claimed). A failing
  fast CLI read is one such failure: `get_cli_sessions(fast_window=True)`
  propagates instead of degrading to an empty list, because an empty CLI list
  would silently omit every CLI/agent row the full builder returns.
- Archive/paged/`all_profiles`/source-filter shapes, and `visible_only=False`,
  keep the full builder synchronously.
- Legacy schemas: `read_fast_sidebar_agent_rows` mirrors the full reader's
  degradation — no `messages` table → denormalized counts + `started_at`; no
  `messages.session_id` → denormalized counts; no `messages.timestamp` →
  `started_at` window; `limit=None` (the `all_profiles` projection) → delegates
  to the full reader.
- A read-only open failure returns an empty list (the fast reader must never
  create or write the store; the full reader instead falls back to a writable
  open, so where the read-only open fails but the writable open succeeds, the
  full reader still reads rows the fast reader omits). That request's fast
  payload carries no CLI/agent rows until the background full rebuild lands:
  the same transient divergence class as the bound above, and the one remaining
  silent-degradation path on this reader, kept because the fast path must not
  create the store.
- The fast reader never writes the store: read-only open, no defensive index
  self-heal, no tombstone/prune bookkeeping — the full rebuild owns those
  (SQLite itself may create the store's `-shm`/`-wal` sidecars when it first
  opens a WAL store; that is the open, not a reader write).

## Settings keying

- `show_cli_sessions`, `show_previous_messaging_sessions`, `show_cron_sessions`,
  `show_claude_code_sessions`, `show_webhook_sessions`, and
  `show_kanban_sessions` are part of the route cache key and gate which rows the
  fast payload builds (the JSONL scan included). The settings file stat and the
  settings write version are in the structural stamp, so a settings change
  rebuilds synchronously.
