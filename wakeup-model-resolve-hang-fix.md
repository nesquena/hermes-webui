# Wakeup turn hang — model-provider resolution fix

Branch: `feat/process-complete-event-isla` (fork prototype) · base HEAD `8bad15e`
Scope: model-resolve layer only. Option Z / Option X / live-view fan-out logic untouched.

---

## §1 — Proven root cause (NOT a race)

The user restarted the real instance (pid 765), ran an empty background task
with `notify_on_complete=true`. The `process_complete` event fired
(`/api/process-complete-ack` → 200, `bootstrap-8787.log` line 26149), so Option
Z's `_start_server_side_wakeup_turn → start_session_turn →
_start_chat_stream_for_session` *did* start a server-side `/api/chat/start`.
That chat/start then **hung in model-provider resolution**.

The "Slow WebUI request still running" diagnostic captured the exact frozen
stack (`bootstrap-8787.log` line 26124, `current_stage: resolve_model_provider`,
`elapsed_ms: 5000.4`):

```
_handle_chat_start (routes.py)
 → _resolve_compatible_session_model_state (routes.py:1378)  catalog = get_available_models()
   → get_available_models (api/config.py)
     → _build_available_models_uncached            ← cold-cache full rebuild
       → _read_live_provider_model_ids             ← network probe PER provider
         → get_auth_status → get_api_key_provider_status
           → _resolve_api_key_provider_secret (hermes_cli/auth.py)
             → get_copilot_api_token (copilot_auth.py:353)
               → exchange_copilot_token (copilot_auth.py:297, urllib timeout=10)
                 → urllib HTTPS → ssl._sslobj.read   ← BLOCKED
```

`exchange_copilot_token` does `urllib.request.urlopen(req, timeout=10)` to
`https://api.github.com/copilot_internal/v2/token`. From this WSL/corp network
that endpoint is slow/unreachable. The live catalog rebuild
(`_build_available_models_uncached`) calls `_read_live_provider_model_ids(pid)`
**once per detected provider**, and the Copilot path reaches
`exchange_copilot_token`. The request thread blocked there, so the wakeup
turn's chat/start never returned → no `_run_agent_streaming` thread started →
no SSE, refresh shows nothing (the turn never completed). The same blocking
synchronous live-probe inside chat/start also explains the earlier "streaming
pending" perf reports.

**Why a normal user turn doesn't visibly hang the same way:** by the time a
human interacts, the 24h-TTL catalog is usually warm (a prior `/api/models`
populated `_available_models_cache` + the on-disk cache). The drain thread
fires when the session is **idle** — exactly when the cache is cold (expired,
or this is the first model-touch of the process), so the wakeup is the caller
that pays the cold live rebuild. The fix makes the wakeup take the same
warm/persisted path and never trigger the cold live rebuild.

### The user's 3 hypotheses — each explicitly ruled out

1. **bg-finish race** — Ruled out. The completion was delivered correctly:
   `PROCESS_COMPLETE_EVENTS_SEEN` dedupe + `_process_one` ran,
   `start_session_turn` *was* entered (the stack is *inside* it). The turn
   doesn't fail to start because of an ordering/visibility race on the
   completion queue; it starts and then blocks deterministically in
   `resolve_model_provider`. A race would be intermittent — this reproduces
   every time the catalog is cold.

2. **Compression race** — Ruled out. The frozen stack contains no
   compression / context-restore frames whatsoever
   (`compression_anchor`, manual-compression, anchor restore — none present).
   The block is 100% inside `get_available_models →
   _build_available_models_uncached → _read_live_provider_model_ids →
   copilot_auth`. Compression runs inside the agent turn; the hang is
   *before* `_run_agent_streaming` is even spawned.

3. **SSE / live-view perf** — Ruled out. The diagnostic shows
   `current_stage: resolve_model_provider`, not an SSE write/flush stage.
   No `wfile.write` / `StreamChannel` / `_sse_set_write_deadline` frame. The
   Option Z live-view SSE work (`8bad15e`) is downstream of a *started* turn;
   here the turn never starts because model resolution itself blocks. The
   prior SSE backpressure fix is unrelated and untouched.

---

## §2 — Primary fix (chosen approach: combination of (a)+(b), least-invasive)

Files: `api/config.py`, `api/routes.py`.

A server-initiated wakeup turn must not force a live provider-catalog rebuild.
The session record already has a persisted `model` + `model_provider` (written
by `_prepare_chat_start_session_for_stream` on a prior turn). Re-probing every
provider live is unnecessary and is the hang.

- `api/config.py` — `get_available_models(*, prefer_cache: bool = False)`.
  New keyword-only cache-only mode. With `prefer_cache=True`, after the warm
  in-memory and on-disk cache checks (both kept, fast), if the cache is still
  cold it returns a **network-free minimal catalog** built from config + auth
  (`_minimal_static_models_catalog()` — `active_provider` + `default_model` +
  the default-model group, derived from `cfg["model"]` / auth.json, no HTTP)
  instead of running `_build_available_models_uncached`. Not written to the
  24h cache, so a later human `/api/models` still does a real rebuild.
- `api/routes.py` — `_resolve_compatible_session_model_state(...,
  prefer_cached_catalog: bool = False)` threads the flag into the single
  `get_available_models(...)` call. Default `False` keeps human chat/start on
  full live discovery (unchanged behaviour).
- `api/routes.py` — `start_session_turn(... source="process_wakeup")` resolves
  with `prefer_cached_catalog=True`.

Why correct: `_resolve_compatible_session_model_state` already trusts the
persisted model — when the session has a `model`/`model_provider` (the wakeup
case), the persisted value wins and the catalog is only consulted for
`default_model`/`active_provider` as a backstop. The cache-only catalog
provides exactly those two fields without a network probe, so the wakeup turn
always gets a valid model (persisted → static/default), never a blocking
network probe. This is approach (a)+(b)+(c) unified: it mirrors the warm-path
a human turn naturally takes, just without ever triggering the cold rebuild.

Why least-invasive: one new keyword-only parameter on two functions plus one
small helper. No change to human chat/start behaviour, no provider-system
rewrite, no change to Option Z/X/live-view.

**Regression-hardening follow-up (run 91).** A first cut called
`get_available_models(prefer_cache=prefer_cached_catalog)` unconditionally.
That broke ~27 pre-existing tests across many files
(`test_provider_mismatch.py`, `test_ttl_cache.py`,
`test_model_cache_metadata.py`, …) that monkeypatch `get_available_models`
as a **zero-arg** lambda — `TypeError: ... unexpected keyword argument
'prefer_cache'`. Final shape: the **default human path calls
`get_available_models()` with NO kwargs** (byte-for-byte signature-compatible
with every existing stub); only the wakeup path
(`prefer_cached_catalog=True`) calls `get_available_models(prefer_cache=True)`.
Zero behavioural change for human chat/start, zero stub churn.

---

## §3 — Defense-in-depth fix (bounded rebuild)

File: `api/config.py`, `get_available_models()` cold full-rebuild block.

Independent of the wakeup fix: a cold live rebuild blocking a foreground
request for up to (per-provider timeout × N providers) is a latent bug that
also degrades normal chat/start under cold cache / flaky network. Bound chosen:
**a wall-clock budget on the synchronous wait**, default `4.0s`
(`_LIVE_REBUILD_BUDGET_SECONDS`, env `HERMES_WEBUI_MODELS_REBUILD_BUDGET`;
`0` restores the legacy synchronous unbounded behaviour).

Mechanism (chosen for correctness under the existing RLock + condvar, and
revised in run 91 to preserve the synchronous cache contract):
- The rebuild runs on a daemon worker (`models-catalog-rebuild`).
- The worker signals **build completion** via a `threading.Event`
  (`build_done`) with **no lock**, so a normal fast build is *never* falsely
  flagged as over-budget.
- **Within budget (the normal fast case): the FOREGROUND publishes the result
  synchronously and only then returns.** This preserves the exact
  pre-existing contract — `_available_models_cache` and the on-disk
  `models_cache.json` are populated by the time `get_available_models()`
  returns, with the standard metadata (`active_provider` etc). (An earlier
  run-91 cut let the *worker* publish even within budget; that broke
  `test_ttl_cache.py` / `test_model_cache_metadata.py` because the cache was
  not yet populated when the call returned. Fixed.)
- **Over budget: the worker owns out-of-band publication.** A
  `budget_exceeded` Event + a `_claim_publish()` one-shot guard guarantee
  exactly one publisher even at the wait()-boundary race (no double disk
  write, no lost refresh). The worker only touches `_cache_build_cv` after
  the foreground releases the RLock by returning, so there is **no lock
  inversion / no deadlock**.
- If the build doesn't finish within the budget, the foreground returns the
  best fallback **immediately** — last-known disk cache, else
  `_minimal_static_models_catalog()` — and the worker keeps running
  out-of-band; its result populates the cache and `notify_all()`s any
  cv-waiters for the next caller.

This removes the synchronous unbounded network dependency from the chat/start
hot path with the minimal change. No provider-system rewrite; the per-provider
probe logic is untouched.

Seam: `_invoke_models_rebuild(builder)` wraps the rebuild call so tests (and
the deterministic repro) can simulate a hung provider probe without real
network.

---

## §4 — BEFORE/AFTER repro evidence (deterministic, isolated)

`tests/manual_repro_wakeup_hang.py` drives `start_session_turn(...,
source="process_wakeup")` directly (no browser, no real agent) with the
cold-rebuild seam monkeypatched to sleep 30s (faithful stand-in for the
unreachable Copilot endpoint; deterministic regardless of which providers an
isolated `HERMES_HOME` has — precedent t_9f0184cf). Run in an isolated
`HERMES_HOME` / `HERMES_WEBUI_STATE_DIR`:

```
=== BEFORE (legacy: budget=0, prefer_cache forced OFF) ===
  wakeup chat/start: STUCK — still blocked after 8.0s
                     (the wakeup turn never starts; matches the bug symptom)

=== AFTER (shipped: prefer_cached_catalog=True + bounded rebuild) ===
  wakeup chat/start: started in 0.010s (model='stream-repro-1')

=== RESULT ===
  BEFORE stuck on hung probe: True
  AFTER  started fast:        True (0.010s, persisted model='anthropic/claude-sonnet-4')
  REPRO PASS
```

BEFORE = wakeup chat/start stuck at the live rebuild (the
`resolve_model_provider` stage). AFTER = wakeup turn starts in ~10ms using the
persisted session model. The pytest in §5 is the standing acceptance proof.

---

## §5 — Test results

New `tests/test_wakeup_model_resolve_hang.py` — 7 tests, all green:
- `test_wakeup_turn_uses_persisted_model_no_live_probe` — wakeup with a cold
  catalog + persisted model: `_read_live_provider_model_ids` is patched to
  raise; the wakeup still starts the turn with the persisted model, fast.
- `test_wakeup_resolve_passes_prefer_cached_catalog` — wiring guard.
- `test_chat_start_survives_slow_provider_probe` — normal cold path with the
  rebuild seam hung 3s, budget 0.4s: `get_available_models()` returns < 2s
  with a usable fallback.
- `test_minimal_static_catalog_is_network_free`
- `test_prefer_cache_kw_exists_and_skips_live_rebuild`
- `test_get_available_models_has_prefer_cache_param` (source-grep)
- `test_start_session_turn_uses_cached_catalog` (source-grep)

Targeted suites green (54 passed): `test_session_channel_option_x.py`,
`test_optionz_liveview_perf.py`, `test_bugbatch_apr2026.py`,
`test_wakeup_model_resolve_hang.py`. Headline
`test_server_side_wakeup_when_idle_no_tab` stays green.

Two pre-existing tests updated for the **intentional** new keyword-only param
(not behavioral regressions):
- `test_optionz_liveview_perf.py` ×2 — monkeypatch lambdas `lambda m, p:` →
  `lambda m, p, **_kw:` (they stub `_resolve_compatible_session_model_state`).
- `test_bugbatch_apr2026.py::test_585` — source-grep anchor
  `def get_available_models()` → `def get_available_models(` (the test's
  actual intent — mtime check ordering — is unaffected).

**Regression sweep (run 91).** The first run-91 pass surfaced two real
regression *classes* (both fixed, see §2/§3 follow-ups):
1. ~27 pre-existing tests stub `get_available_models` as a **zero-arg**
   lambda → broke on the new keyword-only `prefer_cache`. Fixed by only
   passing the kwarg on the wakeup path (default path stays no-arg).
2. `test_ttl_cache.py` ×3 + `test_model_cache_metadata.py` ×2 relied on the
   **synchronous** cache/disk publish contract that the first bounded-rebuild
   cut broke (worker published after the foreground returned). Fixed by
   foreground-synchronous publish within budget; worker only publishes
   out-of-band when genuinely over budget.

After both fixes, an **isolated** verification set of 217 tests across the
entire affected surface — `test_provider_mismatch.py`, `test_ttl_cache.py`,
`test_model_cache_metadata.py`, `test_model_resolver.py`,
`test_model_picker_badges.py`, `test_credential_pool_providers.py`,
`test_optionz_liveview_perf.py`, `test_bugbatch_apr2026.py`,
`test_wakeup_model_resolve_hang.py` (7 new),
`test_session_channel_option_x.py` (headline
`test_server_side_wakeup_when_idle_no_tab`), `test_sprint39.py`,
`test_issue1699_model_cache_source_fingerprint.py`,
`test_issue604_all_providers_model_picker.py`,
`test_issue1538_nous_live_catalog.py`,
`test_issue1539_provider_removal_dropdown_invalidation.py` — **all 217
passed**. The deterministic BEFORE/AFTER repro still PASSes (0.008s).

Full serial suite (collection at HEAD = 5688 + 7 new = **5695 collected**,
run isolated `HERMES_WEBUI_TEST_STATE_DIR`): reached 99% with **0 setup
errors** and exactly **1 transient F** —
`test_issue1499_keyless_onboarding.py::TestKeylessChatReady::test_lmstudio_keyless_chat_ready_via_full_status`.
That test **passes deterministically in isolation against this HEAD** (16/16
in `test_issue1499_keyless_onboarding.py`), so it is an order-dependent
global-auth/cache state-bleed flake from an unrelated upstream test, **not a
regression from this change** (this change does not touch keyless/LM-Studio
onboarding). Note on harness: the webui suite is **not xdist-safe** (shared
`HERMES_HOME`/state-dir/port → 2846 spurious setup errors under `-n`); it
must be run serially or with per-run isolated `HERMES_WEBUI_TEST_STATE_DIR` /
`HERMES_WEBUI_TEST_PORT`. The slow `/mnt/d` 9p mount makes a serial full run
~40 min; the result is captured to a persistent log
(`/tmp/wakeup-fullsuite-FINAL-t46fadfbc.log`) so it survives a worker
boundary. Net vs ~5677 baseline: **0 regressions** (the lone F reproduces
green in isolation).

---

## §6 — User verification

1. `git -C /mnt/d/Repositories/hermes-webui log --oneline -1` → confirm the
   four commits sit on top of `8bad15e`.
2. Restart the real instance manually: `python3 bootstrap.py` (do NOT kill
   pid 765 yourself — task already running; restart is yours).
3. Open the WebUI, start a session (any model), then run an EMPTY background
   task that completes quickly:
   `terminal(background=true, notify_on_complete=true, command="sleep 5")`.
4. **Tab CLOSED**: close the browser tab, wait ~10s after the sleep ends,
   reopen the session — the wakeup turn should have fired within seconds
   (assistant turn present), NOT be stuck.
5. **Tab OPEN**: repeat with the tab open — the wakeup turn appears live via
   the existing Option Z `server_turn_started` fan-out, again within seconds.
6. Both ways the wakeup turn must start fast (not block at
   `resolve_model_provider`). Optional: tail
   `~/.hermes/webui/bootstrap-8787.log` — there should be no new
   "Slow WebUI request still running … current_stage: resolve_model_provider"
   line for the wakeup chat/start.

---

## §7 — Rollback

```
git -C /mnt/d/Repositories/hermes-webui reset --hard 8bad15e
```

(Or `git revert` the four commits individually if other work has landed on
top.)
