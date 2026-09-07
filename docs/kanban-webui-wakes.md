# WebUI Kanban wakes

WebUI Kanban wake delivery is disabled by default. It is independent of the browser-toast `notifications_enabled` setting.

Sidecar + HTTP enablement (`GET`/`POST /api/kanban/webui-wake` writing `kanban_webui_wake_state.json` under `STATE_DIR`) is the activation protocol. YAML `kanban.webui_notifier` is comparison only and is not a second gate.

## Enable safely

1. Stop the old WebUI process before upgrading.
2. Back up the state sidecar and every Kanban DB path recorded by the sidecar:
   `kanban_webui_wake_state.json` under `STATE_DIR`.
3. Start exactly one WebUI server for the installation. Exclusivity is SQLite `BEGIN IMMEDIATE` claim CAS plus the in-process poll lock; do not run two WebUI servers against the same board.
4. Read the activation status:
   `GET /api/kanban/webui-wake`
5. In an authenticated same-origin browser request, enable activation with the normal CSRF flow:
   `POST /api/kanban/webui-wake` with `{"action":"enable"}` and the existing `X-Hermes-CSRF-Token` header.
6. Check the response and normal WebUI/server logs. Enable snapshots one event boundary per owned Kanban DB before writing the enabled marker, and advances only `platform=webui` rows this process hosts (blank `notifier_profile` counts as `default` only when this process hosts root). Isolated processes must not wake or baseline foreign profiles. Events accumulated before that boundary do not wake existing subscriptions.

A successful enable is idempotent. The activation boundary is published only after the enabled sidecar write succeeds; a failed baseline or sidecar write leaves the prior activation's in-flight rewind eligibility intact. New subscriptions use the normal current-event cursor initialization.

## Disable and topology changes

Disable with the same authenticated CSRF request and `{"action":"disable"}`. Disabling stops new polling and does not delete subscriptions or events. After board or DB topology changes, disable and enable again so the new topology receives a fresh future-only baseline. Verify the recorded paths with the GET endpoint.

Do not perform SQL cleanup to activate or repair this feature. Do not enable it through browser notification settings.

## Verification coverage

The activation and wake regression matrix is intentionally split at the existing session-scoped server boundary. `tests/test_kanban_webui_wake_activation_e2e.py` exercises the WebUI route boundary, sidecar writes, one-time event boundaries, disable/re-enable, and a subscription added after activation; it skips only when the session server reports its repository Kanban dependency is unavailable. It does not restart the server or attempt a real provider turn.

The in-process tests cover the remaining state and failure dimensions:

- `test_issue1909_csrf_token.py` and `test_issue2572_csrf_diagnostics.py` cover the shared route auth/CSRF gate used by the wake POST.
- `test_concurrent_enable_serializes_one_baseline` and `test_webui_kanban_two_pollers_do_not_duplicate_claim` cover activation and poll serialization.
- `test_enable_is_idempotent_and_disable_reenable_baselines_again` plus `test_missing_or_corrupt_state_is_disabled` cover durable sidecar reload and idempotent state handling.
- `test_poll_rewinds_missing_session_or_profile` covers missing wake targets.
- `test_poll_pauses_when_owned_db_topology_changes` covers topology drift; the E2E new-subscription row covers future-only initialization after re-enable.

A real process restart and a real LLM wake remain outside this fixture boundary: the former is not provided by the session-scoped server fixture, and the latter is blocked by the fixture's isolated network and credentials.

## Rollback

Rollback only to a build that contains this activation gate. Keep the matching sidecar and Kanban DB backups together; never run a pre-gate default-on binary against the live DB. Restoring a DB backup without its matching gated sidecar does not preserve historical-wake safety.

The existing delivery semantics remain unchanged: a hard process death after a cursor claim and before an exact HTTP 200 wake acknowledgement can lose that wake, while accepted/retry races can duplicate one. This feature does not provide a lease, outbox, journal, or exactly-once guarantee.
