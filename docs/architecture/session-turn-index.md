# `GET /api/session/turns` turn index

`GET /api/session/turns` returns a lightweight, metadata-only index of the
visible user turns in one session, so clients can offer outline or
jump-to-turn navigation over long transcripts without downloading the full
message payload. The session's sidecar and state.db rows remain authoritative;
the index is a projection of the same merged message list that
`GET /api/session` serves.

## Request

`GET /api/session/turns?session_id=<id>`

`session_id` is required; a missing or empty value returns
`400 {"error": "session_id is required"}`.

Windowing and resolution parameters are deliberately not part of this
endpoint's contract. `msg_limit` and `msg_before` are ignored (forced off),
because per-turn absolute indices and row counts are only meaningful over the
full authoritative merge. `messages` is ignored (the merge is always
required), `resolve_model` is ignored (no model resolution is performed), and
`expand_renderable` has no effect on the index payload.

## Turn definition and fields

A turn starts at every visible `role: "user"` row of the authoritative merged
message list — the same append-only merge and reconciliation contract as
`GET /api/session`. Hidden user rows (for example `_source:
"delegation_wakeup"`) do not start a turn but remain inside the preceding
turn's span; rows before the first visible user row are excluded.

Each entry in `turns` carries:

- `k` — 0-based turn ordinal.
- `msg_idx` — absolute 0-based index of the turn's user row in the merged
  message array (usable against a full `GET /api/session` load of the same
  transcript state; a live session may grow between the two calls).
- `ts` — the user row's `timestamp` as a float, or `null` when absent or
  non-numeric.
- `preview` — the user row's text, redacted, whitespace-normalized to single
  spaces, redacted a second time, and truncated to at most 80 characters. For
  list-shaped content, only the `type: "text"` parts are joined with newlines
  before the first redaction (the visible preview still normalizes them to
  spaces); images and other parts are ignored.
- `n_msgs` — the number of merged rows inside this turn after the user row
  itself, up to the next visible user row or the end of the transcript.

The envelope is `{"turns": [...], "total_messages": <merged array length>,
"session_id": "<id>"}`. An empty session yields an empty `turns` list. The
payload contains metadata only: no assistant text, no tool payloads, and no
model resolution.

## Preview redaction

The preview is safety-sensitive because it samples user content. The
redaction, normalization, and truncation order is fixed:

1. `_redact_text` redaction of the raw text, honoring the
   `api_redact_enabled` setting. Line-start-anchored patterns (for example a
   `password=...` value at the start of a later line) still see the original
   newlines here.
2. Whitespace normalization (space, tab, newline) to single spaces, so split
   Authorization headers with whitespace before the Bearer keyword are still
   recognized.
3. A second `_redact_text` pass on the normalized text, catching headers the
   first pass skipped because their whitespace split the pattern.
4. Truncation to 80 characters.

Redaction runs before truncation so a cut in the middle of a credential can
never leak a usable fragment in the preview.

## Foreign sessions and profile gates

When the session id has no WebUI sidecar (`KeyError`), the route delegates to
the shared foreign-session synthesizer (`_claim_or_synthesize_cli_session`)
used by `GET /api/session`, and the index is built from the synthesized
messages with the identical envelope. The same profile-visibility gates apply
before anything is served:

- A session owned by a known other profile returns
  `409 {"error": "Session belongs to a different profile", "code":
  "session_profile_mismatch", "session_id": ..., "profile": ...}` so the
  client can offer to switch to it (#5419).
- Unknown, legacy none-profile, and deleted-WebUI (`was_webui`) sessions
  return 404 so the client's self-heal fires (#2782).
- Profile-agnostic sources (Claude Code rows) are exempt from the named
  profile gate.
- A sidecar file naming another session returns 404 (#9019).

Refused requests never claim, rewrite, or cache another profile's session
(#8094, #8834): the cross-profile check runs before the load, and the
backstop visibility check after the load refuses before any write.

## Stability

The payload shape, hidden-row handling, and preview order are pinned by
`tests/test_session_turn_index.py`. Field names and the `n_msgs` semantics
(count of rows after the user row, not including it) are part of the wire
contract the frontend consumes.
