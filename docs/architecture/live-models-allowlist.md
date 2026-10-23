# Live model picker allowlist contract (`/api/models/live`)

This document records the runtime contract for how `/api/models/live` decides
which models a `custom` / `custom:*` provider contributes to the chat model
picker. It describes shipped behavior and changes no runtime behavior. It was
added because the #7165 review flagged the contract as undocumented: the
filtering rules live in `_handle_live_models` (`api/routes.py`) and had only
code comments backing them.

Start here before changing how the live catalog of a custom provider is
filtered, how discovery snapshots are distinguished from allowlists, or what
the endpoint returns when the upstream probe fails.

## Custom-provider read view

The Providers cards, cold and refreshed `/api/models` catalogs, live-model
fallback, reasoning/context metadata and named-custom model repair read custom
entries through `api.config._custom_provider_entries()`. This accepts legacy
`custom_providers` lists and native `providers` mappings without migrating or
rewriting config. Native mapping keys define stable provider IDs; editable
`name` values are display labels. Native `api` / `url` / `base_url`,
`default_model`, credential references and model metadata retain their meaning.
Built-in and plugin provider settings remain separate.

A missing `custom_providers` key permits native fallback. An explicitly empty
or malformed legacy store suppresses that fallback; a non-empty legacy store
wins on overlapping identities while unrelated native entries remain visible.
Disabled native entries are omitted. Among native records, an exact canonical
`providers['custom:<slug>']` key outranks bare or normalized aliases regardless
of insertion order, matching runtime selection. Without that exact key,
competing enabled connection records for one normalized identity fail closed.
The read view omits that unusable identity, leaving unrelated cards, catalogs
and explicit routes available; routing/runtime and explicit context lookup still
reject the requested colliding identity. A display label never silently selects
the first record. Context lookup selects
by identity before URL, and only an identity-absent, unambiguous lookup may infer
a custom owner from its configured URL/model. Its downstream metadata view is
restricted to that selected owner.

Connection/runtime selection still reads the complete original owned record,
not the presentation view, so transport, credential-pool and other advanced
fields cannot be lost. Metadata-only rows remain visible to readers without
thereby declaring a runtime connection. An explicit native disable is terminal
before live-model cache, external catalog or persisted-pool fallback; an absent
config identity may still browse its explicitly persisted pool. Credential
references are resolved within the request's profile environment, without
borrowing a process-global key for an empty named profile.

Named-custom key/capability readers likewise allow an explicitly owned pool
without a presentation row. Raw disabled/ambiguous identity checks run before
pool access. A configured row's key retains precedence; usable pool entries
prefer `runtime_api_key`, then `agent_key`, then `access_token`. An absent own
credential never authorizes another provider's or profile's key, a default
endpoint's key, or ambient/process credentials.

Forced `/api/models` refreshes also probe non-active named/native discovered
snapshots when discovery is allowed, merge live IDs with saved IDs in the
existing picker ordering, and retain saved IDs if probing fails. Explicit
`discover_models: false` still pins the configured snapshot and skips that
catalog probe. `providers.only_configured` compares native mapping keys in this
same stable `custom:<slug>` namespace, including non-active bare-key entries;
built-in/plugin aliases and legacy-store visibility keep their existing rules.

### Native identity and persisted-input compatibility

New cards, catalog `provider_id` values and unqualified native ownership scans
use the canonical named-custom identity `custom:<slug>`, including when the
native mapping uses a bare key such as `providers.myprov`. A native key named
`custom-configured` therefore appears as `custom:custom-configured`, not as a
reserved sentinel for the default endpoint. Non-active picker options carry
that identity as `@custom:<slug>:<model>`; active-provider options may stay bare
while the session retains their provider separately. Editable labels do not
change this identity or split colon-tagged model IDs. Configured object-list
model rows using `id`, `model`, or `name` retain their explicit `label` when
materialized as configured fallback rows in cold catalogs and in the
configured-fallback stage of refreshed catalogs. Live/discovered rows keep
their existing precedence: if an active-endpoint prewarm or live probe has already
supplied the same model, its upstream label may win. This change does not reorder
live and configured model rows.

This is a read-only normalization, not a config/session migration. Existing
bare mapping keys, active-provider values and persisted explicit inputs such as
`model_provider: myprov` / `@myprov:<model>` remain accepted and route to the
same owned endpoint with the complete model ID. An explicit historical hint
may retain its bare spelling in the resolved triple; that does not make it a
second identity newly advertised by the catalog. Built-in/plugin aliases keep
their existing routing rules. The bare `custom` lane remains distinct: when it
represents the configured default endpoint, a named provider cannot take its
URL or credential, even if its name resembles an internal lane description.

### Selected-owner metadata boundary

Context lookup reads the current request/profile config on each call. It passes
only the selected owner's custom-provider metadata to downstream resolvers,
not the full raw list. Two owners sharing a URL and model still have separate
context overrides and keys; an explicit unknown owner receives neither owner's
metadata or key. The selected projection's contents and ownership are the
contract, not its Python list identity. Global `model.context_length` remains
default-model-only; null/malformed provider stores do not authorize borrowing
another provider's connection. Reading these views never rewrites either store.

## Why the endpoint filters at all

OpenAI-compatible gateways (New-API-style aggregators, LM Studio, Ollama
proxies) expose their **entire** catalog from `/v1/models`, including
image-generation, audio, TTS, and embedding models that are not chat models.
The background enrichment path must not surface models the user never opted
into — but it also must not hide models the gateway gained since config was
last edited. The contract below is how those two goals are reconciled.

## The four signals, in evaluation order

For a custom provider the handler consults these signals:

1. **Discovered catalog → no allowlist (live probe wins).**
   When the provider entry carries `models_discovered: true` and discovery is
   allowed (`discover_models` is not an explicit opt-out), the `models:`
   mapping is a *snapshot Hermes persisted from a past discovery*, not a
   hand-curated list. It must not gate the live probe: a model the user pulls
   into LM Studio / Ollama after discovery has to appear in the picker.
   Detected by the shared predicate
   `api.config._provider_models_are_discovered_catalog()` — the same
   predicate the `/api/models` catalog path uses, so both endpoints agree.
   With this signal, no allowlist is derived and the full live catalog is
   returned.

2. **Explicit plural `models:` allowlist → filter.**
   A non-empty plural `models:` value on an entry that is *not* a discovered
   catalog expresses "show exactly these models". The live catalog is
   intersected with it, then any allowlisted model missing from the live
   response is appended (so an allowlisted-but-offline model stays
   selectable). This is the #7165 filter. Only **list** and **string**
   (serialized) shapes are plain allowlists — see "Dict-shaped `models:`"
   below.

3. **Singular `model:` → never gates.**
   The singular field is sticky/default metadata (the model to preselect),
   not an allowlist. A provider configured only with `model: assistant` keeps
   the full unfiltered live catalog; gating on it would collapse discovery to
   a single model.

4. **No allowlist → full live catalog.**
   Preserves the pre-#7165 discovery behavior for setups that rely on it.

### Serialized shapes

`hermes config set` and the JSON-mode editor persist lists as quoted
JSON-array strings (`'["chat-a","chat-b"]'`) or Python literals
(`"['chat-a']"`). The plural value is decoded through the shared
`_parse_config_string_list()` before id extraction — but only when the value
is a `str`; native `dict` / `list` values are walked directly so dict *entries*
keep their `id|model|name` metadata (the decoder stringifies members). A
dict-only parser drops serialized allowlists to "no allowlist" and floods the
picker — the same bug class as `skills.disabled` (#7120 / #7134).

### Dict-shaped `models:`

A dict-shaped `models:` mapping (e.g. `{chat-a: {context_length: 128000}}`)
is *per-model metadata* written by the Hermes Agent setup flow —
`hermes_cli/model_switch.py::_save_custom_provider` and the setup wizard —
**not** a catalog narrow. Treating its keys as an allowlist would collapse the
live picker to the single saved default (keyless Ollama) while the CLI
live-probe shows the full catalog. So a dict is **never** a plain allowlist:

- without an explicit `discover_models: false`, a dict-shaped `models:`
  contributes **no allowlist** and the full live catalog is returned;
- with `discover_models: false` (bool `False` or the string forms
  `"false"`/`"no"`/`"0"`, case-insensitive — mirroring
  `model_switch_providers.py:557`), the dict **keys** are honored as a pinned
  allowlist.

This complements the discovered-catalog rule: `models_discovered: true`
without the opt-out is already ignored (signal 1). The dict guard covers the
Agent-setup shape that is *not* flagged discovered.

If someone later "fixes" the dict shape back into an always-allowlist, the
regression test `test_dict_models_without_discover_false_does_not_gate` fails
and forces the trade-off to be re-argued.

### Empty allowlist semantics

`models: []`, `models: "[]"`, an empty mapping, or a blank scalar are
deliberately treated as **not configured**, never as "allow nothing". The
filter runs only on a non-empty allowlist because gating on declared-ness
would emit an empty picker and make the provider unselectable — a harder
failure than showing a few extra models, and unrecoverable from the UI
(`custom_providers` has no WebUI write path; it is hand-edited in
config.yaml). There is no deny-all use case: a provider the user wants hidden
is removed from `custom_providers` outright.

### Probe failure / empty probe

When the upstream probe fails or returns no models, the endpoint falls back
to `allowlist or config ids` — the non-empty plural allowlist first, so a
singular `model:` outside the allowlist cannot leak in precisely when the
probe is unavailable; with no allowlist, the configured ids (singular +
plural) are served. A discovered catalog therefore still serves its saved
models on a transient probe failure instead of emptying the picker.

## Tests

- `tests/test_issue3718_live_models_custom_probe.py` — real-handler
  (`_handle_live_models`) coverage of every signal above: plural list /
  JSON-array-string / Python-literal / list-of-dicts filtering, dict-shaped
  `models:` (not gated) and dict + `discover_models: false` (pinned),
  singular passthrough, malformed safety, discovered-catalog passthrough,
  `discover_models: false` pinning, and both fallback branches.
- `tests/test_issue7404_models_discovered_not_allowlist.py` — the
  `/api/models` side of the discovered-catalog contract.
- `tests/test_byok_model_dropdown.py` — picker rendering, profile-scoped
  caching, and current-session model preservation around this endpoint.
