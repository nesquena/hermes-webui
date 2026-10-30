# Composer draft persistence

Composer drafts use the single upstream store: `sessions/.drafts/<sid>.json`.
A dedicated record, including an explicit empty record, overlays the embedded
`composer_draft` compatibility snapshot. A damaged record retains upstream
recovery: warn for malformed JSON/non-object payloads and fall back to the
embedded draft. There is no quarantine or second draft authority. Successful
full owner saves refresh the in-memory fallback from their serialized embedded
snapshot, not merely from draft reads. Concurrent saves retain independent
temporary files and replacements; a short fallback-publication lock and the
written file revision prevent a delayed older save from regressing that fallback.
Ordinary draft POSTs never acquire this publication lock.

## Request ownership and cost

An ordinary POST validates/coerces the text and attachment list, checks subagent
ownership, reads fixed-identity ownership metadata, and takes the draft
lifecycle lock, plus the short compare/publication section described below.
It resolves ownership again under that lock before merging a partial update.
It never acquires the transcript writer lock, calls the full
Session resolver/load, rewrites an existing transcript, or touches its recency
or index. Equal payloads are no-ops. Draft GET uses the same read-only owner
resolver, and both methods reject unsafe identifiers with HTTP 400. The atomic
writer escapes lone Unicode surrogates without changing ordinary UTF-8 output.

Draft authorization is bound to the caller's active profile, captured at handler
entry before body processing or the first visibility guard, including cookie-less
requests that use the process default. The guard and the entire draft branch
reuse that value. Both branches reject a foreign durable owner before reading
its draft, even if the full Session cache still holds a default-profile owner.
POST revalidates against that same profile under the lifecycle lock and at every
compression successor; GET reads under the lifecycle lock. The freshly loaded
owner's profile never becomes the request's authority.

The resolver retains the exact durable owner's inode/size/mtime/ctime revision.
Draft writes prepare and fsync an independent temporary file, then compare that
revision and replace the draft under a short owner-publication gate used by
both indexed and skip-index Session saves. Owner replacements hold shared mode
and can overlap one another. Draft compare/replace, first-shell compare/create,
cooperative owner unlink and joint revision validation hold exclusive mode.
Admission uses FIFO tickets, batching consecutive shared operations. Later
arrivals cannot overtake either a waiting checkpoint or a waiting draft
publication; exclusive tickets wait for all admitted owners to drain. Interrupted
waiters remove their tickets and wake successors. Modes are never nested or upgraded.
Revision churn retries the complete
owner resolution and draft projection at most four times, using the same captured
profile and SID. A foreign owner, lost authority, or exhausted budget returns a
generic 409 without publishing. GET validates its read revision before returning;
POST unchanged and compare-mismatch results retry a changed revision too. This is
optimistic ordering: a save may complete while a draft is reading or fsyncing;
it never waits for the draft lifecycle, transcript scan, encoding, fsync or HTTP
response. The gate covers publication, not index updates or embedded
fallback publication. Guarded draft and first-shell replacements reacquire the
gate and revalidate their condition for each Windows retry, sleeping outside it.
Ordinary owner replacements preserve the two-argument `_safe_replace` seam and
hold shared mode through its Windows retry loop: at most five attempts with
50/100/200/400 ms delays (750 ms total requested backoff, excluding filesystem
latency). Other admitted shared owners may still run; exclusive waiters, and new
shared tickets queued behind them, wait for that owner to finish.
Missing durable owners fail closed; an empty draft for a never-persisted new chat
remains a diskless no-op.

The ownership cache retains only eleven metadata fields and is bounded to 128
file revisions. The path/inode/size/mtime/ctime key invalidates it on replacement
or external metadata edits, even when the full session is cached. The read-only
profile gate shares these validated facts without promoting a Session object.
Current writer headers contain every required field before the transcript. The
reader validates the complete header up to the first `messages` array opener,
using last-value semantics for repeated header keys. Merely seeing each field
once does not authorize a write; malformed/truncated headers fail closed. The
transcript body beyond that intentional modern boundary is not parsed.

Old/custom layouts with missing fields require one bounded-memory streaming
scan through EOF per uncached file revision. The reader must establish absent
provenance, so this first scan is transcript-sized and can be slower than a full
JSON decode. Warm requests, even without a cached Session object, reuse the
same revision's facts. Replacement, external edits, cache eviction or restart
can require another legacy scan; this is not an unconditional first-save-only
latency guarantee. Normal modern headers remain transcript-size-independent.
This compatibility path does not recover, self-heal or cache a transcript object.

Subagent preflight retains the state.db source check and persisted
source_tag/raw_source/session_source precedence. Persisted metadata is checked
again before writing; all other routes keep their original shared preflight.

## Compression and lifecycle synchronization

Session property reads use a draft identity pinned at construction. A temporary
snapshot session_id rebind does not change it; permanent compression ownership
publication updates it, and rollback restores it. Draft responses use their
request-local payload rather than assigning into a possibly rotated cached
object. Export explicitly resolves the public property, with a safe default
for session-like objects lacking that property.

Snapshot-authority walks retain the transcript lock, bounded hops, durable
profile/parent/continuation edge checks, target-lock revalidation, fail-closed
responses, and `session_moved` replay contract. Every edge metadata read retains
its prior file revision; the entire bounded read set is validated together under
the publication lock before a redirect. Scans and HTTP output stay outside it.
A snapshot discovered after
ordinary preflight releases the draft lock before escalating to the transcript
lock. Compression marker/publication and rollback hold the old identity's draft
lifecycle lock, so an ordinary in-flight draft precedes the marker or observes
its durable authority on recheck.

First meaningful New Chat drafts still publish the small owner once under
transcript -> draft -> cache lock ordering. Encoding and fsync precede a locked
compare-and-create: a competing owner is never overwritten. The route re-resolves
and re-authorizes that durable SID before projecting the draft, whether creation
won or collided. An empty draft does not materialize
a shell. Successful saves/loads distinguish persisted owners from new shells;
a deleted persisted owner cannot be recreated as an unsaved shell.

The fixed 64 lifecycle lock stripes are reentrant so a route may hold one across
read/merge and invoke the existing atomic writer/removal helpers. Single and
bulk deletion hold that same lock across owner unlink and draft deletion.
Secondary removal paths (background completion, ephemeral success/cancel and
admission rollback) remove both files. Existing failure reporting and orphan
retry scanning remain; failed draft unlink does not abort other cleanup.

Bulk cleanup waits at most 100 ms for each transcript writer lock, skips a busy
owner without loading/evicting it, and reports `skipped`. Orphan retry transcript
lock waits use the same bound. Once admitted, a cleanup may still wait for an
in-flight small draft publication; this is not a whole-request deadline.

Thread locks coordinate the existing single WebUI process, not independent
multiprocess servers. Cross-tab writes remain last-arrival-wins. The existing
file fsync/atomic replace durability and local-filesystem trust assumptions are
unchanged; no directory-fsync or symlink-confinement guarantee is added.
