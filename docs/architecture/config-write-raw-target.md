# Config write-target contract (raw, un-expanded YAML)

This document records the contract every `config.yaml` writer in this
repository must satisfy: **a write transaction reads and saves the raw,
un-env-expanded document.** It describes shipped behavior and changes no
runtime behavior. It was added because the rule previously lived only in code
comments, and because the skills toggle carries one deliberate exception that
an undocumented exception invites re-"fixing" as a bug.

Start here before adding a config writer, changing a writer to compare against
a value it just loaded, or touching `_load_yaml_config_file_raw()` /
`_save_yaml_config_file()`.

## The rule

`_load_yaml_config_file()` is a raw parse followed by `_expand_env_vars()`
(`api/config.py`). A writer that reads through it and then calls
`_save_yaml_config_file()` persists the **expanded** structure. So a writer
must load with `_load_yaml_config_file_raw()` and mutate and save that:

```python
config_data = _load_yaml_config_file_raw(config_path)
# mutate config_data in place
_save_yaml_config_file(config_path, config_data)
```

`_load_yaml_config_file_raw()` returns a deep copy by default, so a writer
cannot accidentally mutate the shared memoized parse. `_save_yaml_config_file()`
evicts the `(path, mtime, size)` parse cache entry on its own write, so the
next read re-parses the bytes just saved.

## Why it matters

Two failure modes, both silent:

1. **Baked secrets.** `api_key: ${OPENAI_API_KEY}` becomes `api_key: sk-...`
   in plaintext `config.yaml`, permanently. The indirection is destroyed, so a
   later environment rotation no longer takes effect for that profile.
2. **Cross-profile leak.** Expansion resolves against whichever profile
   environment is thread-local-active. A writer running under profile A can
   therefore persist profile B's secret into A's `config.yaml`.

`set_max_tokens()` already transacted on the raw loader; this contract extends
the same rule to every other writer.

## Compare resolved, persist raw

The raw-load rule does **not** mean comparing raw text. A writer that must
decide *what to change* has to compare in the same vocabulary the reader uses,
then persist the raw form. Comparing a raw persisted value against a resolved
runtime value is handled **at the comparison site**, never by re-widening the
transaction back to the expanded document:

- The **reader's** normalization is `_expand_env_vars()` first, then parse a
  scalar serialized list (`_parse_config_string_list`), then strip
  (`_normalize_names_list` / `_normalize_disabled_set`). A writer that skips
  either step mis-compares.
- A padded env value must be stripped before comparison. With
  `DISABLED_SKILL=" demo "`, an unstripped `res != name` keeps the entry and
  the API returns `ok: true` while the skill stays disabled.
- Only rewrite a key when its resolved value actually changed. Otherwise a
  `${VAR}` is replaced by its expansion on an unrelated write.

Every other caller of a raw-loaded config already passes resolved values; only
the raw-loaded write path sees raw ones. That asymmetry is why the comparison
sites, not the loader, owe the conversion.

## The skills toggle, and its one exception

`_handle_skill_toggle()` edits `skills.disabled` and, when that key exists,
`skills.platform_disabled.webui` (the agent checks platform-specific lists
first when `HERMES_SESSION_PLATFORM` is set). Both go through
`_toggle_name_in_list()`, which derives the effective names the way the reader
does and returns the value in **raw** form so `["${DISABLED_SKILL}"]` still
matches its resolved name while the `${VAR}` stays on disk.

**The exception — a scalar `${VAR}` that expands to several names
cannot survive a partial edit.** If `skills.disabled: ${DISABLED}` holds
`DISABLED='["demo","other"]'`, the raw field is one entry. Saving it back as a
list item means the reader no longer parses that entry as a serialized list, so
enabling `demo` would silently enable `other` as well. Instead the remaining
resolved names are written explicitly, and the rewrite is logged via
`logger.info` with a message saying the placeholder was dropped.

This is deliberately narrow:

- It applies to **skill-name lists only** — the values are names, never
  secrets, so replacing a placeholder with explicit names cannot bake a secret
  onto disk. No other field may use this path.
- A placeholder that survives the edit keeps its raw bytes: entries are
  returned raw, and only the entry whose resolved-and-stripped name matches is
  dropped.
- The rewrite is logged so an operator who later expects an environment change
  to affect the remaining names can see that the indirection is gone.

## Verification

`tests/test_issue5619_config_write_authority.py` is the regression matrix for
this contract — writer preservation, read-compare-resolves, and the toggle's
reader-vocabulary cases. `tests/test_issue5619_config_write_contract_doc.py`
gates this document itself, so the rule cannot silently go undocumented.

## Scope notes

- The three MCP write handlers are deliberately **not** covered by the matrix;
  they own a separate write-transaction lane and the maintainer asked for no
  parallel fix alongside it.
- An architectural `${VAR}` guard inside `_save_yaml_config_file` is tracked
  separately; it would move this rule from "every writer must remember" to
  "the saver refuses to persist an expansion". Until it lands, this document
  is the enforcement.
