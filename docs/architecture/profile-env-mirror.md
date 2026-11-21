# Profile env mirror and background-worker scopes

Current contract for how a background worker's profile environment is projected
onto the shared process environment, and what happens when two workers overlap.
Start here before changing `profile_env_for_background_worker`,
`_apply_profile_env_to_process`, or the credential-name scrub (#9187).

## Why a process-wide mirror exists

WebUI runs several profiles in one server process. A detached worker (a
background title, a streaming checkpoint, a manual compression) runs outside the
request thread that established its profile, so it binds the session profile in
two channels:

- **Thread-local** (`api.config._thread_ctx.env`, plus the context-local
  `hermes_constants.set_hermes_home_override`) — authoritative for WebUI
  helpers and Agent config reads.
- **Process-wide** (`os.environ`) — a compatibility mirror for readers that
  still call `os.getenv()` directly.

The mirror is what makes overlapping scopes hard: `os.environ` has exactly one
value per name, but two concurrently running workers can want different views
of the same name.

## Two name sets per scope

Every scope claims names in two directions:

- **Visible** — its `safe_runtime_env` (`get_profile_runtime_env` filtered for
  gateway parity, plus `HERMES_HOME`). These are mirrored in so the body can
  read its own configuration and credentials.
- **Hidden** — every credential env name the server's profile configs declare
  (`_all_profiles_secret_env_names`: the provider/agent registries plus each
  profile's `custom_providers`/`providers` `key_env` and `${VAR}` API keys).
  Names the scope does not make visible are scrubbed, so one profile's work
  cannot resolve another profile's key through an `os.environ` fallback
  (`agent.secret_scope` misses fall through to the process env because WebUI
  runs the Agent with multiplexing off).

The **secret scope itself stays per-profile** and is built from the profile's
own runtime env. The process mirror is a compatibility layer, not the
authority for credential resolution.

## Overlapping lifetimes

A per-scope snapshot/restore is wrong under overlap: the second worker's scrub
erased a still-running worker's credential, and a later exit restored a value
that belonged to a worker which had already finished. Scopes therefore register
their claims in a per-name registry and the process value is recomputed from
the live set:

- Any live scope claiming a name **visible** keeps it readable; the most recent
  claim wins when two visible claims disagree (the process env can hold one
  value only).
- When every live claim is **hidden**, the name is removed.
- When the last claim leaves, the name returns to the process baseline captured
  on its first claim.

The recompute is commutative, so entry and exit order do not matter and no
scope restores another scope's stale value. Only setup/restore run under
`_ENV_LOCK`; the body is never serialized.

**Consequence for deployments:** while a worker for another profile is live, a
name that scope claims visible stays readable process-wide — that is the
direct-`os.getenv()` compatibility contract. A deployment value (an operator
env var) is unaffected unless some profile's `config.yaml` declares the same
name as a credential; in that case it is hidden inside every worker body that
does not make it visible.

## Cost

Each worker entry unions every profile's declared credential names. That
declaration is cached per `config.yaml` under `(path, mtime_ns, size)`, so a
Settings save is observed on the next entry and unchanged files are not
re-parsed. The registry part is recomputed per call (module imports are
already cached by the interpreter).
