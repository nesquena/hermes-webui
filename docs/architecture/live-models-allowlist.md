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
