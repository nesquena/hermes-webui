# Locale direction metadata

Hermes WebUI locale bundles may declare an optional `_dir` metadata field:

```js
_dir: 'rtl',
```

`setLocale()` applies the selected locale to the root document:

- `lang` uses `_speech` when present, preserving the existing behavior.
- `dir` uses `_dir` when present and otherwise explicitly restores `ltr`.
- `data-locale` records the resolved locale key for locale-scoped styling and tests.

Technical-content isolation and the Persian locale bundle are intentionally handled in a follow-up change so this infrastructure PR remains small and independently reviewable.
