# Session archive cross-profile contract

Current contract for `POST /api/session/archive`: which profiles a request may
touch, what the request fields mean, and which failure each refusal returns.

Start here before changing the archive handler, the all-profiles archive path,
or the profile-visibility rules that guard session mutations (#7549, #7731).

## Why the route owns its own boundary

`/api/session/archive` is exempt from the generic
`_guard_request_session_visibility` check (`_request_session_visibility_exempt`).
That exemption is deliberate, and it is why the handler enforces the boundary
itself.

Archiving a session that lives in another profile publishes a WebUI sidecar
owned by that profile. On the *next* request — the restore — the generic guard
finds that sidecar and answers `409 session_profile_mismatch` before the
archive handler ever runs. Without the exemption, archiving a session would
permanently brick its own restore, and sessions already imported from another
profile could never be un-archived.

The tradeoff: this route no longer inherits the guard, so it restates the same
contract explicitly. Keep the two in step — a change to the guard's semantics
(`#7710`) must be reflected here.

## Request fields

| Field | Type | Meaning |
|---|---|---|
| `session_id` | string, required | Session to archive or restore. |
| `archived` | bool, default `true` | `false` restores the session. |
| `all_profiles` | bool, default `false` | Opt in to reaching a session outside the active profile. Requires `profile`. |
| `profile` | string | The owning profile the caller claims the session belongs to. Validated against `_PROFILE_ID_RE`. |

`all_profiles` is never a standalone permission. It only widens the *search*,
and only when `profile` names a validated owner. The frontend sends both
together, and only while the merged all-profiles sidebar view is active
(`static/sessions.js`).

## Invariants

1. **A cross-profile lookup requires a claimed, validated owner.**
   `all_profiles: true` with an absent or malformed `profile` is rejected
   *before* the resolver runs. Without this, a caller who knew a foreign
   session ID could materialize and archive it across the profile boundary —
   `all_profiles` alone widened the search and both owner checks were
   conditional on a truthy requested profile.

2. **The shared materializer stays active-profile-only.**
   `_get_or_materialize_session()` is the chokepoint for rename, regenerate,
   update, move, prompts, chat-start and anchor scenes. It must never pass
   `allow_all_profiles=True`, or one unqualified request from profile A can
   create a persistent sidecar for profile B's session, rename it, and copy B's
   transcript into it.

3. **A refused request never mutates the session.**
   The profile boundary is evaluated *before* `get_session()` and before
   `_session_is_subagent_view_only()`, both of which call `get_session()` and
   repair legacy sidecar layouts in place (`#8094`). A request that will be
   refused must leave the sidecar bytes, the `.json.bak` file, and the session
   cache untouched.

4. **The archived session keeps its CLI origin.**
   The lock-held `Session.load(sid)` reload reads the sidecar that
   `import_cli_session` saved — before the caller assigns source fields — so the
   resolved `cli_meta` is retained and re-applied after the reload. Otherwise
   the save persists a WebUI-native copy (`is_cli_session=False`, null source).

## Profile rules

| Request | Session owner | Result |
|---|---|---|
| unqualified | active profile | archived (`200`) |
| unqualified | other profile, has sidecar | `409 session_profile_mismatch` |
| unqualified | other profile, no sidecar | `404` — the CLI lookup stays active-profile-only |
| unqualified | unknown/legacy `None`-profile sidecar | `404`, so the frontend's stale-URL self-heal keeps firing |
| `all_profiles` + matching `profile` | any profile | archived (`200`); also what makes a foreign session restorable |
| `all_profiles` + wrong `profile` | another profile | `409 session_profile_mismatch`, no sidecar written |
| `all_profiles` + absent `profile` | — | `400 profile is required for all_profiles archive` |
| malformed `profile` (any request) | — | `400 invalid profile` |
| `all_profiles` in isolated mode, claim names the pinned profile | pinned profile | archived (`200`) — the flag is ignored, never widened |
| `all_profiles` in isolated mode, claim names another profile | — | `403` |

Isolated-profile mode never widens the search. It degrades to an ordinary
active-profile archive when the claim names the pinned profile, so a saved
`_showAllProfiles` toggle restored by the browser cannot strand the pinned
profile's own sessions behind a 403.

## Failure responses

| Status | Body | Meaning |
|---|---|---|
| `400` | `{"error": "invalid profile"}` | `profile` present but malformed. |
| `400` | `{"error": "profile is required for all_profiles archive"}` | `all_profiles` without an owner claim. |
| `400` | `{"error": "Read-only imported sessions cannot be archived from WebUI"}` | Target is read-only. |
| `400` | `{"error": "Subagent sessions cannot be archived from WebUI"}` | Delegated child (`#5307`). |
| `403` | `{"error": "all_profiles archive is not allowed in isolated profile mode"}` | Widening attempted under isolated mode. |
| `404` | `{"error": "Session not found"}` | No such session in any permitted profile. |
| `409` | `{"error": "Session belongs to another profile", "code": "session_profile_mismatch", ...}` | Profile conflict. `profile` carries the real owner. |

A `409` is reserved for a session that *exists* under a different profile. A
session that exists nowhere is `404`. The resolver prefilters a mismatched owner
to `{}`, so the handler re-probes without the profile filter before answering
`404` — otherwise callers could not tell a profile conflict from a missing
session.