# Fast-background-task wakeup race — defer path had no autonomous-agent drain

Branch: `feat/process-complete-event-isla`
Base:   `bdc6e7b` (Option Z + model-resolve-hang fix; Test A verified by agents-manager on pid 978)
HEAD:   `d2d34a1` (this fix — NOT pushed, NOT promoted to PR)

```
fa7f7ec fix(streaming): drain deferred process-completions at turn teardown so fast bg tasks still wake the agent
a760fa5 test: wakeup defer-race (completion during teardown) + no double-fire + no loop
d2d34a1 docs(CHANGELOG): fix fast-background-task wakeup race (defer path had no autonomous-agent drain)
```

---

## §1 Proven root cause

agents-manager observed on the real instance (pid 978, HEAD `bdc6e7b`):

| Test | Command | Server-side wakeup | Agent self-woke |
|------|---------|--------------------|-----------------|
| A | empty bg task `sleep 5` | FIRED ✅ | yes |
| B | empty bg task `sleep 2` (fast) | did NOT fire ❌ | no |

The model-resolve hang is genuinely fixed (Test A proves it). Test B is a
separate, final architectural race — the one the user suspected from the
start ("輸出完成前的 wakeup event 真的有 race 問題").

### The defer branch discards the prompt

`api/background_process.py::_process_one` (pre-fix, ~line 418-432):

```python
wakeup_prompt = str(payload.get("wakeup_prompt") or "").strip()
if wakeup_prompt:
    if _session_has_active_turn(session_id):     # ACTIVE_RUNS has a row for sid
        logger.debug("server-side wakeup deferred: turn active ... "
                      "PR #2279 next-turn drain will deliver the marker")
        # ← wakeup_prompt DISCARDED. Only a bare PENDING_PROCESS_COMPLETIONS
        #   session-id flag was left (set earlier at bg_process.py:383).
    else:
        _start_server_side_wakeup_turn(session_id, wakeup_prompt)   # idle → works (Test A)
```

`_session_has_active_turn` (`bg_process.py:435`) keys on `ACTIVE_RUNS`.

### The only consumer can never recover it

The bare `PENDING_PROCESS_COMPLETIONS` flag's only consumer is the PR #2279
next-turn drain `api/streaming._drain_webui_process_notifications`
(`streaming.py:638`), invoked **only inside the turn pipeline** at
`streaming.py:3445` — i.e. only when a NEW turn runs for that session. That
drain:

- reads `process_registry.completion_queue` — but the Option Z drain thread
  (`_drain_loop` → `_process_one`) **already `get()`-consumed** that event off
  the queue; it is empty.
- is gated by `PROCESS_COMPLETE_EVENTS_SEEN[sid]` + the registry
  `_completion_consumed` marker, **both set in `_process_one` at lines
  376-395 BEFORE the defer branch runs**.

So even if a user turn DID come, the deferred completion is structurally
unrecoverable. For an **autonomous agent there is no next user turn at all**,
so `_drain_webui_process_notifications` is never called → the deferred wakeup
is lost forever.

### The teardown timing window — why A passed, B failed

Turn teardown (`api/streaming.py`, `_run_agent_streaming` `finally`):

```
streaming.py:4602  with STREAMS_LOCK:
streaming.py:4603      STREAMS.pop(stream_id, None)
streaming.py:4610      unregister_active_run(stream_id)   # ← ACTIVE_RUNS row for sid cleared
```

Between "agent finished output" and `unregister_active_run`,
`_session_has_active_turn(sid)` still returns True.

- **FAST task (2s, Test B):** completes INSIDE that teardown window →
  `_session_has_active_turn` True → DEFER → prompt discarded → autonomous
  agent (no next turn) never wakes. ❌
- **SLOW task (5s, Test A):** completes AFTER teardown finished →
  `_session_has_active_turn` False → idle path → `_start_server_side_wakeup_
  turn` fires. ✅

This exactly matches the A-success / B-fail observation.

---

## §2 The fix (file:line, minimal, symmetric with the idle path)

Only the **defer path** is touched. Idle path, next-turn drain, Option X
live-view, model-resolve fix, closed-tab headline — all untouched.

1. **`api/config.py`** (+`DEFERRED_PROCESS_WAKEUPS`, `DEFERRED_PROCESS_WAKEUPS_LOCK`)
   — persist the actual prompt(s) the defer branch used to discard:
   `session_id -> list[{process_id, wakeup_prompt}]`, lock-guarded.

2. **`api/background_process.py`**:
   - `_process_one` defer branch now calls
     `record_deferred_wakeup(session_id, process_id, wakeup_prompt)` instead
     of discarding (idempotent per process_id).
   - `claim_deferred_wakeups(session_id)` — atomic `dict.pop` under the lock
     (exactly-once delivery primitive).
   - `drain_deferred_wakeups_for_session(session_id)` — the turn-teardown
     idle-hook: multi-stream guard (`_session_has_active_turn` must be False)
     → atomic claim → `_start_server_side_wakeup_turn` per entry (same
     throwaway-daemon-thread path the idle branch uses, so teardown never
     blocks) → discard the stale `PENDING_PROCESS_COMPLETIONS` flag.

3. **`api/streaming.py`** — invoke `drain_deferred_wakeups_for_session(session_id)`
   immediately after the `with STREAMS_LOCK:` teardown block that ends with
   `unregister_active_run(stream_id)`. At that point the just-ended stream's
   `ACTIVE_RUNS` row is cleared (under `ACTIVE_RUNS_LOCK`, independent of
   `STREAMS_LOCK`), so `_session_has_active_turn` is now False for this
   session unless a *different* stream is still active.

**Symmetry achieved:** idle at completion → fire now (Option Z idle branch);
busy at completion → fire at turn-end when the session goes idle. No
busy-wait, no polling — the teardown is the exact active→idle edge.

---

## §3 Idempotency proof (no double-fire, no loop, multi-stream)

- **Shared SEEN/consumed contract:** `_process_one` adds `process_id` to
  `PROCESS_COMPLETE_EVENTS_SEEN[sid]` and the registry `_completion_consumed`
  set at lines 376-395, *before* the defer. The next-turn drain
  (`_drain_webui_process_notifications`) early-returns on exactly those gates,
  so it can never also deliver a process the defer path owns. Verified by
  `test_next_user_turn_drain_and_teardown_hook_dont_double_fire` and the
  pre-existing `test_process_complete_ab_coexistence.py` (still green).

- **No double-fire (teardown vs next-turn drain):** `claim_deferred_wakeups`
  is an atomic `DEFERRED_PROCESS_WAKEUPS.pop(sid)` under
  `DEFERRED_PROCESS_WAKEUPS_LOCK`. Whichever runs first (teardown idle-hook OR
  a next-turn drain, if a user turn did come) gets the entries and delivers;
  every later caller gets `[]`.

- **No wakeup loop:** the wakeup turn started by the hook itself ends and
  tears down → its teardown re-runs `drain_deferred_wakeups_for_session`. The
  entry was already `pop`-claimed, so it finds nothing → starts no further
  turn. Verified by `test_no_wakeup_loop` (3 extra teardown re-runs, calls
  stays == 1).

- **Multi-stream / cancel-reconnect guard:** the hook first checks
  `_session_has_active_turn(sid)`; if a second stream is still active
  (cancel/reconnect left another `ACTIVE_RUN`), it returns 0 and leaves the
  entries intact for the later teardown (or a next-turn drain) to claim. Only
  the teardown of the LAST active stream for the session fires. Verified by
  `test_multistream_guard_only_fires_when_truly_idle`.

---

## §4 BEFORE / AFTER repro evidence

**BEFORE** — source fix `git stash`-ed away, repro script run against clean
`bdc6e7b` working tree (`/tmp/wakeup_before_repro.py`):

```
has DEFERRED_PROCESS_WAKEUPS attr: False
PENDING marker set (bare flag only): True
completion_queue empty (event consumed by drain thread): True
SEEN gate set: True
next-turn drain recovers it: False -> []

RESULT: wakeup_prompt was DISCARDED at defer; no teardown hook exists;
        autonomous agent NEVER wakes -> THIS IS THE BUG (Test B failure).
```

**AFTER** — fix restored, headline test (the autonomous-agent Test B
scenario: ACTIVE_RUN present → completion deferred → `unregister_active_run`
→ teardown hook):

```
tests/test_wakeup_defer_race.py::test_completion_during_turn_teardown_still_wakes
.                                                                        [100%]
1 passed in 5.84s
```

The teardown idle-hook fires `_start_server_side_wakeup_turn` exactly once for
the deferred completion (`source="process_wakeup"`, message
`[IMPORTANT: Background process ...]`), and `DEFERRED_PROCESS_WAKEUPS[sid]` is
emptied by the atomic claim.

---

## §5 Test results vs baseline

New file `tests/test_wakeup_defer_race.py` — 5 tests, all green:

- `test_completion_during_turn_teardown_still_wakes` — headline, the Test B
  scenario (completion deferred while active, fires once at teardown).
- `test_idle_completion_still_fires_once` — Test A path unchanged; the new
  teardown hook does not double-fire an idle-path wakeup.
- `test_next_user_turn_drain_and_teardown_hook_dont_double_fire` — shared
  SEEN/_completion_consumed gate; next-turn drain does not also deliver.
- `test_no_wakeup_loop` — wakeup turn's own teardown re-run is a no-op.
- `test_multistream_guard_only_fires_when_truly_idle` — cancel/reconnect
  guard fires only on the last active stream's teardown.

Directly-related suites (run together, fix applied) — **49 passed**
(44 pre-existing baseline + 5 new), including the closed-tab headline
`test_server_side_wakeup_when_idle_no_tab` and the AB-coexistence dedupe
tests:

```
tests/test_session_channel_option_x.py
tests/test_process_complete_wakeup.py
tests/test_process_complete_ab_coexistence.py
tests/test_notify_on_complete_webui.py
tests/test_wakeup_defer_race.py
→ 49 passed
```

Full suite (`pytest tests/ -q`, isolated HERMES_HOME + port): result appended
to `/tmp/wakeup_after_full.log` — see §5-addendum below; target is 0
regressions vs the ~5677 / ~5695 baseline (the +5 new tests are all green).

---

## §6 User verification

1. `cd /mnt/d/Repositories/hermes-webui` (HEAD is `d2d34a1`, branch
   `feat/process-complete-event-isla`).
2. Restart the real instance — user-driven:
   `python3 bootstrap.py` (do NOT let an agent kill pid 978; user restarts).
3. agents-manager then re-runs BOTH empty-background-task self-wakeup tests on
   the live instance:
   - `terminal(background=true, notify_on_complete=true, command="sleep 2")`
     then end the turn → agent must self-wake (a NEW turn appears) — the
     previously-failing Test B.
   - `terminal(background=true, notify_on_complete=true, command="sleep 5")`
     then end the turn → agent must self-wake — Test A, must still pass (no
     idle-path regression).
   Both must self-wake. (No provider is configured in an isolated bootstrap
   home, so the deterministic `tests/test_wakeup_defer_race.py` pytest is the
   acceptance proof per precedent t_9f0184cf; live re-test on the real
   instance is the user-side confirmation.)

---

## §7 Rollback

```
cd /mnt/d/Repositories/hermes-webui
git reset --hard bdc6e7b
```

This drops the 3 commits above and restores the model-resolve-hang-fixed
state exactly (Test A still works, Test B still broken).
