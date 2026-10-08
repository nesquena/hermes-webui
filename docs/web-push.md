# Web Push (closed-app notifications, iOS/iPadOS)

Opt-in. Without `pywebpush` or VAPID keys the feature is invisible and inert.

1. `pip install pywebpush` (in the interpreter that runs the WebUI).
2. `python scripts/generate_vapid_keys.py --subject mailto:you@example.com`
   writes `<state-dir>/webui_vapid.json` (0600). Or set
   `HERMES_WEBUI_VAPID_PUBLIC_KEY`, `HERMES_WEBUI_VAPID_PRIVATE_KEY`,
   `HERMES_WEBUI_VAPID_SUBJECT`. Restart the WebUI.
3. Serve over HTTPS (e.g. `tailscale serve`). iPhone/iPad: iOS 16.4+, Share ->
   Add to Home Screen, open from the icon.
4. Settings -> Preferences -> "Push when the app is closed" -> Enable, then
   "Send test push", then close the app.

Notifications: response complete, approval needed, clarification needed,
background task complete. Subscriptions live in
`<state-dir>/webui_push_subscriptions.json` (0600) and apply to the whole
instance (one password = one trust domain). Endpoints are SSRF-guarded
(https, globally-routable addresses only, connection pinned, no redirects/proxies).
API: `GET /api/push/status`, `GET /api/push/vapid-public-key`,
`POST|DELETE /api/push/subscribe`, `POST /api/push/test` (all require auth).
