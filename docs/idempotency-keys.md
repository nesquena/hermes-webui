# Idempotency keys for `POST /api/chat/start`

External automation (scripts, gateways, retry loops) can crash or lose the
HTTP response *after* the WebUI has admitted a turn. A plain retry would
start a second turn with the same prompt. Sending an **idempotency key**
with the request gives the server a durable, exactly-once guard: the first
request atomically claims the key *before* any agent turn is admitted, and
every later request with the same key and the same logical request replays
the original acceptance instead of starting a new turn.

## Supplying a key

A key may be supplied on either transport; the body field wins when both
are present:

| Transport | Location | Example |
|---|---|---|
| Header | `Idempotency-Key` | `Idempotency-Key: chat-2026-10-29-1` |
| JSON body | `idempotency_key` | `{"session_id": "…", "message": "…", "idempotency_key": "chat-2026-10-29-1"}` |

Key rules:

- printable ASCII (`0x21`–`0x7E`), trimmed, at most 200 characters —
  a pipe (`|`) is not allowed;
- a **present-but-invalid** key (empty, whitespace, too long, non-string,
  disallowed characters) is refused with `400`, never silently downgraded
  to the no-key path;
- omitting the key entirely keeps the legacy behaviour (no idempotency
  guarantee; the normal stream/session duplication guards still apply);
- the server resolves the *active profile* itself and namespaces the key
  under it — a client-supplied profile hint is never trusted, and the same
  raw key in two profiles is two independent guards.

## Semantics

A keyed request is bound to a **fingerprint** of the side-effect-relevant
request fields (`session_id`, `message`, `attachments`, `workspace`,
`model`, `model_provider`, `explicit_model_pick`, `regenerate`,
`regeneration_revision`, `moa_config`, `keep_count`, `prompt`,
`prompt_index`, and the server-resolved profile). Presentation-only fields
such as `client_ts` are excluded.

- **First request**: the key is claimed durably (a provisional turn
  identity is persisted with the claim), the turn is admitted, and on
  success the acceptance and final identity are recorded durably.
- **Retry with the same key + equivalent request** (the lost-response
  case, or a concurrent duplicate): the original acceptance is replayed —
  `200` with the original `session_id` / `stream_id` / `turn_id`, marked
  with `"replayed_from_idempotency_key": true` — and **no second turn
  starts**. This works while the first turn is still running, after it
  completes, and after a WebUI restart (the guard lives on disk).
- **Same key + a different request**: refused with a deterministic `409
  idempotency_conflict`; no turn starts.
- **Retention**: records (and their completion tombstones) are kept for
  `24h` (`DEFAULT_TTL_SECONDS`), after which the key is refused with
  `410`, never silently re-admitted.
- **Replay respects session visibility**: the replayed acceptance carries
  the stored session's identity, so a retry must pass the same
  session-profile visibility boundary as the keyless path. If the session
  now belongs to another profile the replay is refused with `409
  session_profile_mismatch` (or `404` when ownership cannot be
  established) — an idempotency key is not an authorization credential.

Crash windows (the claims a restart can recover from disk alone):

- a crash **between the claim and the turn being admitted** leaves a
  pending record whose turn may never have run — the retry is refused
  with `409 idempotency_outcome_unknown` (no `retry_after_seconds` hint),
  telling the caller to check the session before retrying;
- a crash **after admission but before the completion write** does not
  guarantee a replay: if the admitted identity was never durably
  attached, the restarted store still finds only the pending record and
  answers `409 idempotency_outcome_unknown` — it does not invent a
  provisional or fabricated stream identity. The retry replays the
  admitted identity only once the persisted record proves it. That is
  distinct from a **live** completion-write failure, which is reconciled
  onto the pending claim while the process is still running and then
  replays the admitted identity instead of starting a second turn;
- a turn that *was* completed and whose record then expired refuses the
  key with `410` — the message states whether the turn completed or its
  outcome was never recorded.

## Response codes

| Status | `code` | Meaning |
|---|---|---|
| `200` | — | Fresh admission, or replay (`replayed_from_idempotency_key: true` in the payload). |
| `400` | — | A key was supplied but is malformed (empty / whitespace / too long / non-string / disallowed characters). |
| `409` | `idempotency_conflict` | Same key, different request — the key is already bound to another fingerprint. |
| `409` | `idempotency_in_flight` | Same key, same request, still PENDING in this process — includes `retry_after_seconds: 1`. |
| `409` | `idempotency_outcome_unknown` | Recovered pending claim with no recorded outcome (crash window). No retry hint — do **not** blind-retry; check the session or use a new key. |
| `409` | `session_profile_mismatch` | The replayed session belongs to a different profile (same refusal the keyless path gives). |
| `410` | `idempotency_key_expired` | The key's retention window elapsed (24h default); the turn may have run. |
| `503` | `idempotency_store_unavailable` | The durable claim store could not be read or written. Three distinct cases: a failure writing the **initial claim** (nothing admitted yet) is rolled back — no turn starts and the key is *not* retained, so the same key may be retried as a fresh request; a failure recording an **already-admitted** turn keeps the guard — the turn IS running but its acceptance could not be made replayable, so retry with the same key (the retry replays the admitted turn or is refused, never duplicated); a store that simply **cannot be read** fails every keyed request closed, so no duplicate turn can be admitted. |
| `503` | `idempotency_replay_unavailable` | The stored result is a stale failure; it was released so the same key can be retried as a fresh request. |
| `404` | — | The replayed session cannot be found / its ownership metadata is unreadable (keyless parity). |

Every error payload that concerns a key echoes it back as
`"idempotency_key": "<key>"`.

## Durable storage

The guard lives under the WebUI state directory:

- `<state_dir>/idempotency/store.json.wal` — append-only write-ahead log;
  every claim / completion / release is one JSON line, fsync'd before the
  request is acknowledged;
- `<state_dir>/idempotency/store.json` — the compacted snapshot, rewritten
  atomically (temp file + `rename` + directory fsync) only when the WAL
  crosses its compaction threshold.

On startup the snapshot is loaded once and the WAL is replayed on top, so
a crash mid-write can neither corrupt the guard nor lose a claim that was
acknowledged. Records are bounded (10 000 by default; a full store fails
closed with `503` rather than evict a live claim), and completion
tombstones are kept outside the record cap so a full store cannot
silently re-admit a turn that already ran.