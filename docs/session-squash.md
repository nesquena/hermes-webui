# Archive and squash a conversation

Session squash is a deliberate, **destructive change to the live WebUI transcript**. It is not the same as automatic context compression (`/compress`), clearing a session, or merely hiding a session in the archive. The full pre-squash sidecar is kept in a verified gzip archive; the visible transcript and model context become a single summary. The operation does not delete the archive.

## Operator workflow

1. Stop any running turn and wait for its writeback to finish. Archive the conversation in the session list and select it (enable **Show archived** if necessary). Do not squash a conversation that has a child/fork or is a sealed compression parent; use its authoritative continuation instead.
2. On desktop, use **Squash conversation** in the composer controls. On a narrow/mobile layout, open the composer configuration actions and choose **Squash conversation**. Confirm the preview for the selected conversation. A new message or any other change since preview invalidates that confirmation.
3. Reopen the conversation to verify that one summary remains. The UI reports success or failure but does **not** display the archive name or digest; an operator with access to the server must inspect the completed job response (`GET /api/session/squash/status?job_id=...`, with the job ID from the start response) or the archive manifest in the WebUI state directory and record its `archive_name`, `source_sha256`, and `squashed_sha256` before a later restore. If the auxiliary summarizer is unavailable, the generated summary is explicitly a fallback template; check it before using the conversation again. Do not delete the archive until you no longer need the original transcript.
4. A failed job is not proof that the live sidecar changed; inspect its actual state before retrying. Background job status is in process memory and expires, so the durable manifest/archive, not the browser toast, is the long-term recovery record.

Only archived, writable, idle WebUI sessions that are authoritative lineage tips are eligible. The operation refuses active/pending streams, unreleased writeback owners, live Agent turn leases, foreign profiles, changed sidecars, and unreadable lineage. It does not squash CLI-only records or descendants with active children. A successful squash retains a fork-parent link; a compression-snapshot parent is detached so its old transcript cannot be stitched back into the display.

## Persistence and recovery contract

The WebUI session sidecar in the active state directory is the live source. `POST /api/session/squash/preview` with `{"session_id":"..."}` returns an authority object containing `profile`, `canonical_path`, `session_id`, `lineage_tip`, `source_sha256` and `message_count`. `POST /api/session/squash` echoes that exact object as `confirm` (and optionally supplies `summary`); it returns a `job_id` to poll via `GET /api/session/squash/status?job_id=...`. These routes use normal WebUI authentication and profile visibility checks. A supplied summary must meet the server's minimum length. Preview is read-only; start is asynchronous and can still fail after admission.

Before committing, the worker verifies the archived-tip/profile authority, source digest and file identity again. A per-session process lock serializes squash and restore; the claim/publish step refuses to overwrite a concurrent ordinary sidecar writer. The checksum-verified archive and manifest live under the WebUI state directory's `session-squash-archives/<session_id>/`. The manifest records the original digest and byte count, the squashed digest/generation, archive filename, and the Agent state row IDs affected by the barrier. A failed commit attempts to restore the exact original sidecar, sidebar index, cached session and any already-archived state rows. If rollback itself fails, inspect the logs and the preserved claim/archive rather than retrying blindly.

The summary sidecar records `intentional_shrink_generation` so startup `.bak` recovery does not mistake a pre-squash backup for newer content. Its `truncation_watermark` and `truncation_boundary` prevent earlier Agent `state.db` rows from appearing in the visible transcript. Existing active state rows for the same session are soft-archived (`active=0, compacted=1`) inside one immediate SQLite transaction, only when no live turn lease owns the session; missing required schema/lease authority fails closed. This does **not** add a new state ingestion tombstone to Hermes Agent or make a multi-database filesystem/SQLite transaction atomic across process crashes. The separate #6600 projection work must be checked for compatibility when it lands.

## Restore drill (advanced; no UI button)

Use only after verifying the conversation has had **no subsequent writes**. Retain the original `archive_name`, `source_sha256` and the exact post-squash `current_sha256` reported as `after.sha256` in the completed job. Submit `POST /api/session/squash/restore` with:

```json
{
  "session_id": "<session id>",
  "archive_name": "<archive filename, not a path>",
  "confirm": {
    "session_id": "<same session id>",
    "source_sha256": "<original digest>",
    "current_sha256": "<squashed digest>"
  }
}
```

Use an authenticated request in the matching WebUI profile; do not send credentials in chat or shell history. Restore verifies the archive checksum and manifest, session/profile ownership, the current sidecar's exact digest and squash generation, and the state rows recorded in the manifest. A renamed, edited, newly replied-to or re-squashed sidecar is **not** an untouched squash result and restore refuses it rather than discarding later turns. On success the original sidecar bytes and matching index/cache are restored, the recorded soft-archived rows are reactivated, and a separate archive preserves the former squashed state. Verify the resulting transcript and index before continuing. On failure, preserve the archive and consult the server logs; do not manually overwrite the sidecar or delete `.bak`/claim files without a separate recovery plan.
