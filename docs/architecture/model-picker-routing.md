# Model Picker Routing

Current identity, restoration, and selection rules for the model picker.

## Provider-scoped identity

A model option is identified by both its provider and its model ID. The same model ID under two providers must remain two selectable options, and only the option for the selected provider can appear active or carry the Selected badge.

## Mistral provider identity

Use `mistral` for the WebUI provider ID. The legacy provider IDs `mistralai` and `mistral-ai` are still accepted when reading existing settings. This does not rename vendor prefixes in model IDs such as OpenRouter routes.

For an accepted built-in route, when multiple `providers:` keys normalize to `mistral`, the picker uses the first matching enabled record in configuration order (`enabled: false` records are skipped). Model discovery and routing take their settings from that same record; they do not borrow credentials from a later alias. The separate ambiguity guard can still reject a legacy-spelled request when enabled built-in providers collide. Prefer one `providers.mistral` record rather than keeping several aliases.

The selected record is chosen on its enabled state alone, not on whether it declares an endpoint. When that record declares no `base_url`, the provider's own default endpoint applies: model discovery falls back to the vendor endpoint for that provider, and routing falls back to the active model's `base_url` when the active model names the same provider. A later alias is never consulted for the missing endpoint, so a record that is missing its endpoint does not cause the request to be paired with another alias's credential or URL.

## OpenRouter route identity

OpenRouter route identity is case-insensitive and may include the WebUI's optional `@openrouter:` wrapper. All other route text must match exactly.

Do not replace dashes with dots, split a vendor path, or remove a suffix such as `:free` or `:thinking`. OpenRouter can expose distinct upstream IDs that differ only by dash or dot spelling, and the picker must preserve both routes.

Other providers keep their existing identity normalization for custom-provider compatibility.

## Overflow restoration

When a saved OpenRouter model is present in a provider group's overflow catalog, the picker promotes the exact matching entry into that group and removes the same entry from the hidden overflow list. Later restorations must reuse the promoted option.

Promotion must preserve the catalog model ID, label, provider, and suffix. It must not consume a different entry that normalizes to the same route.

## Synthesized fallback

The picker may synthesize a provider-qualified option only when the exact OpenRouter route is absent from both the visible group and its overflow catalog. Repeated restoration of that missing route must reuse the synthesized option.

The fallback remains routable. It does not add or replace a catalog option with a different route.

## Selection and live-catalog updates

Option deduplication, live-catalog merging, and selected-row rendering use the same provider-aware route identity. A later catalog update must not collapse or select a punctuation-distinct OpenRouter sibling.

## Configured badges and the Configured section

The configured-badge lookup and the Configured section key share one identity: within a provider, the `@provider:` routing prefix and then that provider's own `provider/` segment are peeled, and any other leading segment (for example `anthropic/` under OpenRouter) stays part of the model ID. With no provider on either side, the legacy normalized key applies.

The configured-row dedup (`_isEquivalentConfiguredModelEntry`) keeps its own, narrower rules, so the lookup can show a configured badge on a row that the dedup does not treat as the configured ID (for example a typed `openrouter/<vendor>/<model>`). The native configured ID is then synthesized as well, and the Configured section collapses the two:

- One configured entry is one Configured row. Rows showing the same entry (the badge's own provider, role and label) share a slot, so a providerless badge is one row too.
- A row that sends the configured ID itself wins the slot over one that sends `provider/<model>` (a row is sent as its value less only `@provider:`). Otherwise plain IDs win over provider-qualified ones, then the shorter spelling.
- A hidden overflow row leaves the "Show all" count only when a Configured row sends the same ID, so a `provider/<model>` twin stays reachable.
