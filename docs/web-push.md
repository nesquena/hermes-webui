# Web Push (closed-app notifications, iOS/iPadOS)

Opt-in. Without `pywebpush` or VAPID keys the feature is invisible and inert:
no push delivery workers or cron observer are started. There is no frontend build
step and no required push-service account.

## Enable this device

1. `pip install pywebpush` in the interpreter that runs the WebUI. For a managed
   Agent runtime launched with `python -I`, an alternative is an operator-owned
   dependency directory installed with `pip install --target <directory> pywebpush`
   and explicitly configured as `HERMES_WEBUI_PUSH_DEPS_DIR=<directory>`. Do not
   modify the managed runtime itself. `PYTHONPATH` alone does not work under `-I`.
2. `python scripts/generate_vapid_keys.py --subject mailto:you@example.com`
   writes `<state-dir>/webui_vapid.json` (0600). `<state-dir>` is resolved the
   same way as the server: `$HERMES_WEBUI_STATE_DIR` if set, otherwise
   `<HERMES_HOME>/webui` (default `~/.hermes/webui`); `--state-dir` overrides.
   The generator uses the existing `cryptography` P-256 dependency; only delivery
   requires `pywebpush`. It does not print the private key or overwrite existing
   keys without `--force`. Alternatively configure `HERMES_WEBUI_VAPID_PUBLIC_KEY`,
   `HERMES_WEBUI_VAPID_PRIVATE_KEY`, `HERMES_WEBUI_VAPID_SUBJECT`. Restart the WebUI.
3. Serve over HTTPS (e.g. `tailscale serve`). iPhone/iPad: iOS 16.4+, Share ->
   Add to Home Screen, open from the icon.
4. Settings -> Preferences -> "Push when the app is closed" -> Enable, then
   "Send test push", then close the app.

Push bodies transit Apple/Google/Mozilla. Text is always credential-masked
**before** shortening, regardless of `api_redact_enabled`. Set
`HERMES_WEBUI_PUSH_SNIPPETS=0` to send generic completion text with no reply snippet.
If the redactor is unavailable, text is dropped rather than sent unmasked.

## Routing and storage

Response-complete, approval-needed, clarification-needed and background-task
notifications target devices that opened/started the owning session. Enrollment
requires readable session ownership visible to the request's profile; an unknown,
unreadable or foreign-profile session cannot enroll a device. On first continuation
of a CLI/TUI/Desktop session, enrollment follows successful WebUI materialization
and profile validation, before the run starts. The server stores
only a hash of the per-browser `X-Hermes-Push-Device` identifier, not the raw id.

Subscriptions live in `<state-dir>/webui_push_subscriptions.json` (0600), and
session owners in `webui_push_session_owners.json` (0600, bounded). One instance
password is one trust domain, but this does not authorize broadcasting unowned
sessions. Legacy unowned subscriptions receive nothing until their device
rebinds them. `HERMES_WEBUI_PUSH_BROADCAST_UNOWNED=1` is an explicit administrator
opt-in for **session** events only; it never authorizes cron fanout.

Endpoints are SSRF-guarded: HTTPS, globally-routable addresses only, delivery-time
DNS revalidation, connection pinning, no redirects or proxies. Delivery uses at
most two daemon workers, ten-second request timeouts and a 32-item queue. Producers
use a nonblocking enqueue; full queues drop notifications rather than blocking
SSE settlement or creating a retry backlog. Repeated approval heads are coalesced
before scheduling; approval preparation and delivery use this same pool, with no
per-notification threads.

## Cron delivery (advanced API opt-in)

Cron runs in another process, and the gateway session watcher parks without SSE
clients and excludes cron. A separate read-only observer therefore watches the
Agent's existing `<profile-home>/cron/output/<job-id>/*.md` publication seam
(`cron.jobs.save_job_output`). No Agent patch, gateway/SSE connection, or browser
poll is needed. A persisted output is reported as **Cron output ready**, not as
proof that the job succeeded. Success and error outputs can both generate it.

Cron is **off by default even on a push-enabled device**. Enrollment is currently
API-only; there is no additional Settings control. Select the intended profile in
the WebUI, enable normal Web Push on the device, then run this in that app's browser
console (it neither prints nor copies the endpoint or key material):

```javascript
const registration = await navigator.serviceWorker.ready;
const subscription = await registration.pushManager.getSubscription();
if (!subscription) throw new Error('Enable Web Push on this device first');
const response = await fetch('api/push/cron', {
  method: 'POST', credentials: 'same-origin', headers: _webPushHeaders(),
  body: JSON.stringify({endpoint: subscription.endpoint, enabled: true})
});
if (!response.ok) throw new Error('Cron notification enrollment was rejected');
```

`POST /api/push/cron` requires auth, the strict push CSRF token, a valid device
header, an endpoint already owned by that device, and a JSON boolean `enabled`.
The home comes only from the request's trusted active-profile context, **not** a
client-supplied path or cron job name. Repeat with `enabled: false` to revoke only
that profile's cron consent. `GET /api/push/status?endpoint=...` returns
`cron_subscribed` for this device's currently selected profile, never profile paths,
owner hashes, other endpoints or private keys.

The stored consent pins the **cron-store owner home**, not a job's optional
execution `profile` (which selects config for the run). Jobs with identical ids in
different homes remain separate, including one-shot jobs removed from `jobs.json`
after execution. Request cookies, profile switches and transient `HERMES_HOME` or
`cron.jobs` module globals cannot redirect the observer. No unowned broadcast,
inferred session owner, or automatic consent on boot is used.

The observer starts only on an enabled server, ticks every two seconds, and scans
at most 128 directory entries per enrolled home per tick using resumable,
no-symlink, descriptor-relative walks. At most eight distinct homes and 32 device
subscriptions may be enrolled. Per home it retains at most 1024 recent output
identities; capacity retires the oldest timestamp rather than retaining an
unbounded history. Reopening the app/rebinding or rotating an endpoint preserves
that device's consent; transferring an endpoint to a different device does not.

Cron sends only generic text through the same forced-redaction payload builder
and bounded delivery queue. It never reads prompts/results, job names, or output
content. Internal profile routing metadata is stripped before network delivery.
Opt-out/unsubscribe, endpoint ownership and profile consent are rechecked immediately
before **each endpoint's send**, including after an earlier endpoint stalls in the
same fanout. 404/410 responses prune the subscription and its cron consent; unsubscribe is
idempotent. An already in-flight network request may finish after revocation.
Unreadable/corrupt stores fail closed and are not overwritten.

This is best-effort notification, not a durable cron delivery ledger. Existing
history at consent/server startup is not replayed. Files restored with old mtimes,
outputs pruned before observation, very late publications older than the bounded
retention floor, queue overflow, server downtime, and temporary delivery failures
can drop a notification. Scan wall-clock time is not a completion watermark:
atomic rename may publish a file whose mtime predates the previous scan. Run one
observer-serving WebUI process per state directory; independent replicas do not
coordinate deduplication. On platforms without safe descriptor-relative directory
access, cron observation fails closed. Regular in-app Tasks/history remains the
source of truth; cron notification clicks open the app, not an invented session.

## Acceptance evidence

Automated checks prove local routing/queue/payload behavior, **not** operating-
system delivery. No device verification is claimed by this table.

| Maintainer item | Automated coverage | Device evidence still needed |
| --- | --- | --- |
| Closed-app completions, approvals, clarification, background tasks | `test_web_push.py`, `test_web_push_owners.py`, `test_web_push_review_fixes.py`: producer wiring, targeting, deduplication, SW source handlers | Installed PWA closed/backgrounded, permission granted, receive each event and tap it |
| Closed-app cron | `test_web_push_cron.py`: separate-process atomic output, live observer-to-delivery-worker without SSE, profile pinning, late publication, history suppression | Real scheduled Agent job while installed PWA is closed, then tap and inspect Tasks |
| Optional install | `test_web_push.py`, `test_web_push_deps_dir.py`, `test_web_push_cron.py`: absent keys/dependency no-op, managed `-I` opt-in directory, disabled startup has no observer | Fresh install without extras has no push controls; opt-in install works |
| Forced payload redaction | `test_web_push_review_fixes.py`: API redaction off, cut-straddling secrets, missing redactor, generic snippets; cron never reads result content | Inspect received notifications; no credential/client-content disclosure |
| Profile/device-safe subscriptions | `test_web_push_owners.py`, `test_web_push_cron.py`: device isolation, strict CSRF, legacy migration, foreign-session denial, two homes with identical job ids, symlink rejection, profile-specific status and consent | Two profiles/devices, profile switch and server restart; no cross-delivery |
| Bounded off-SSE delivery | `test_web_push.py`: slow sender off producer thread, full queue never blocks; `test_web_push_cron.py`: bounded scans/cache, worker handoff and shutdown | Close PWA, stall network and run multiple jobs; no chat/settlement slowdown |
| Revocation lifecycle | `test_web_push.py`: both 404 and 410 prune, repeated unsubscribe; `test_web_push_cron.py`: queued opt-out/unsubscribe, rebind/rotation/transfer, corrupt-store fail-closed | Browser permission revocation, subscription expiry and re-enable after key rotation |

API summary (all authenticated): `GET /api/push/status`,
`GET /api/push/vapid-public-key`, `POST|DELETE /api/push/subscribe`,
`POST /api/push/test`, `POST /api/push/cron`. Mutation routes require the explicit
push CSRF token when password auth is enabled, including non-browser requests.
