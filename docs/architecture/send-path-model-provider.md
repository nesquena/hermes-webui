# Send-path model provider contract (chat start)

This document records the runtime contract for **which provider a chat turn is
sent to**, as resolved by the browser before `POST /api/chat/start`. It
describes shipped behavior and changes no runtime behavior. It was added
because the #7865 review flagged the contract as undocumented: the precedence
rules live in `_modelProviderForSend()` (`static/ui.js`) and had only code
comments backing them.

Start here before changing provider precedence for outgoing turns, the picker's
explicit-pick evidence, or how a restored session's provider is resolved.

## Why the send path resolves a provider at all

The model name alone is not always enough to route a turn: when two providers
offer the same bare id (for example `gpt-5.5` under both OpenAI and OpenAI
Codex, or `grok-4.3` under both a custom endpoint and `xai-oauth`), the
provider decides credentials, endpoint, and protocol translation. The payload
field `model_provider` is therefore resolved in the browser by
`_chatPayloadModelState()` (`static/messages.js`), which delegates to
`_modelProviderForSend(model)`.

## The precedence order

`_modelProviderForSend(modelId)` evaluates these sources in order:

1. **An explicit provider embedded in the model id (`@provider:model`).**
   Authoritative. A qualified id is the user's (or a caller's) exact intent and
   needs no other evidence.

2. **The dropdown's option provider — only with session-scoped pick evidence.**
   When the currently selected picker option describes the model being sent,
   its provider wins over the session's stored provider **only if** the picker's
   session-scoped "explicit pick" marker exists (see below) and still names the
   option that is selected. A bare dropdown match is not evidence of a pick:
   after a session restore, the catalog repaint can leave another provider's
   identically-valued option selected (for example `gpt-5.5`, offered by both
   OpenAI and OpenAI Codex), and letting that option win would route the turn to
   a provider the user never picked (#7865). The marker is written by the
   picker's change handler for the active session and cleared on session
   load/switch, so a restored session keeps its own provider (#7860).

3. **The session's stored `model_provider`.** Always authoritative for a loaded
   session whose provider the session record itself holds — including every
   restored session, because a restore is not a pick.

4. **The persisted model state (localStorage), when it matches the model
   being sent.** Used for the empty composer / fresh-session case.

5. **`null`.** The server then applies its own compatible-model resolution.

## The explicit-pick marker (why it exists)

A bare dropdown match is **not** evidence of intent. After a session restore,
`syncTopbar()` runs before the catalog refresh (`static/sessions.js`), so
another provider's identically-valued option can be left selected while the
session correctly holds its own `model_provider`. Letting such an option win
routes the turn to a provider the user never picked — either a provider error
or an answer from the wrong provider (#7865).

The marker closes that gap. It is:

- **Written** by the picker's change handler (`$('modelSelect').onchange` in
  `static/boot.js`) for the active session, recording the picked value and its
  provider.
- **Read** by `_modelProviderForSend()` and compared against both the active
  session id and the model actually being sent, so a marker can never authorize
  an override for a different session or a different model.
- **Cleared** on every session transition: `loadSession()` clears it for the
  session being left and for same-session force-reloads (a reload is a load —
  the reloaded session's own provider stays authoritative), and `newSession()`
  clears it for the replaced session.
- **Not consumed by send().** Unlike the `_pendingSessionModel` family (the
  one-shot explicit-pick signal that `send()` reads and then clears, #3739),
  this marker survives for the whole session: the evidence "the user picked in
  the dropdown for this session" stays true until the session changes.

Storage is `sessionStorage` under `hermes-webui-explicit-picker-pick:<sid>`,
so it is scoped to the browser tab and to one session id.

## States that must hold

| State | Resulting provider |
|---|---|
| Qualified id `@claude:model-x` sent | `claude` |
| Bare id sent, matching dropdown option, pick evidence for this session | the option's provider (#7860 fix) |
| Bare id sent, matching dropdown option, **no** pick evidence (restored session, collision across providers) | the session's provider (#7865 fix) |
| Pick evidence exists but belongs to another session id | the session's provider |
| Pick evidence exists but was recorded for a different model value | the session's provider |
| Bare id sent, dropdown says nothing about it | the session's provider |
| No session, matching dropdown option | the option's provider (empty composer) |
| No session, no dropdown match | persisted state's provider, else `null` |

## Import and materialize

Opening a Desktop/CLI session copies the provider onto the sidecar before the
send path runs. Lookup order is `model_config.gateway_runtime.provider`, then
`model_config.provider`, then `billing_provider` when that value is a real
provider. `auto` and `custom` are billing buckets, not identities, and stay
unset so chat start still uses the profile provider.

An effective `gateway_runtime` route ends the lookup, as it does in the Agent
(`hermes_state_gateway`): a named route is the answer, and a bare `custom`,
`auto` or `default` route leaves the provider unset. The older
`model_config.provider` and `billing_provider` were replaced by that route, so
falling through to them would pin a provider the session no longer uses. The
projection has no profile, so it cannot map the runtime's endpoint to a named
identity; leaving it unset gives the profile-provider behavior of master.

The recent-session list publishes that value as `model_provider`. A saved
link to an older session is not in that capped list. The single-row claim
path (`_claim_or_synthesize_cli_session`) reads the same columns, so that
open does not drop the provider either.

Re-import fills a missing sidecar provider only when the stored model still
matches the imported model. A provider already set on the sidecar is not
overwritten, and a later model change does not inherit the old Desktop
provider.

Every import, materialize, archive, claim and re-import site saves that
provider only when the owning profile (`cli_meta["profile"]`) can route it
(`_routable_imported_model_provider`). Otherwise the sidecar keeps no provider
and the first send uses the profile provider, as before #8249, instead of
failing on a route the profile no longer has. The check is:

- the profile's own `model.provider`: kept;
- `custom:<slug>`: kept only when a record in that profile's config owns the
  slug (`_select_custom_provider_record`) **and** the send path's own verdict
  accepts it: the runtime lookup followed by
  `merge_custom_provider_runtime_bundle`, with no route error. A relay removed
  from `config.yaml`, a record with a key but no endpoint
  (`custom_provider_endpoint_unresolved`), and one whose declared `key_env` does
  not resolve (`custom_provider_credential_unresolved`) are dropped. Keyless
  endpoints and keys from the profile `.env`, a pool or a `key_cmd` stay
  routable;
- bare `custom`: kept only when it is the profile's own provider;
- any built-in: kept only when the Agent's `resolve_runtime_provider`
  returns a credential for it under that profile. A `providers:` entry is not
  enough on its own: an empty entry, an endpoint-only entry or an unresolved
  `${VAR}` key gives the send path no credential. A missing login or key, or a
  resolver that cannot be loaded, drops it.

The runtime lookup runs inside `profile_scope_for_detached_worker` for the owning
profile (the request profile when the row names none), including when that
profile is already the request profile. Request-profile TLS alone does not
apply a named profile's `.env`, where its keys usually live.

The list projection itself is not checked, because it is cached and built for
every sidebar row. The check runs where the provider is written, so a config
edit after the list was cached still applies.

## Tests

- `tests/test_7860_model_provider_send_precedence.py` — drives the real
  `_modelProviderForSend` under node, including the partial-catalog restore
  case, cross-session leakage, and stale-value cases.
- `tests/test_chat_start_provider_fallback.py` — payload contract plus the
  pick-evidence gate from the `_chatPayloadModelState` seam.
- `tests/test_issue7860_model_picker_split_qualified_id.py` — how a qualified
  id is split into model + provider by `_modelStateForSelect`.
- `tests/test_7865_settings_default_provider_survives_qualified_save.py` —
  the Settings save path keeps the provider when a qualified option is saved
  as the default (same feature, adjacent regression).
