# Incremental live semantic parsing


The stream attachment in `static/messages.js` owns incremental semantic readers.
Raw `assistantText` and channel-only `reasoningText` remain authoritative. Parser
state is derived, attachment-local, and discarded with the attachment; it is not
serialized, shared across transports, or a renderer/projection cache.

## Views and boundaries

- The live reader applies XML tool suppression before code-aware thinking-tag
  extraction. A finite-state XML recognizer accepts plain and DSML-prefixed tags,
  including arbitrary whitespace. Pending delimiters have a current-prefix view;
  parsing does not stop and leave publication behind retained raw content.
- The persistence reader does not strip XML. It preserves the batch extractor's
  durable grammar: an unfinished **nonleading** thinking tag remains literal
  content, while a leading unfinished block is reasoning. Closing a block moves
  its content to reasoning without losing the prefix or suffix.
- Post-tool segments retain upstream's **XML-only** grammar and raw offset.
  Resetting a segment creates a fresh XML reader. This intentionally does not
  reinterpret a delimiter that straddles an already accepted raw tool boundary.
- `syncInflightAssistantMessage()` publishes immediately, before the active-pane
  guard, using the durable view. Existing persistence throttling is unchanged.
- Recovery uses explicit `lastReasoningText`, including an empty string, rather
  than feeding the message's already-merged inline reasoning back into the channel.
- Tool start and completion reconcile the semantic boundary before sealing prose
  or applying the tool event. Current synchronous combined painting is unchanged.
- Recovery, non-append replacement, and terminal partial delimiters may use the
  retained batch extractor. These are explicit boundaries, not token-time scans.

The XML batch grammar removes leading whitespace as soon as either XML keyword
occurs, even in literal prose. This can retroactively reinterpret an earlier
indented thinking tag. The incremental reader handles that transition with one
rebuild from the trimmed retained prefix; later deltas continue incrementally.

## Ordered XML grammar

The batch oracle's replacement order is authoritative: complete blocks, then
orphan/truncated openings and tails, then malformed DSML prefixes, then leading
whitespace. The first pass reads raw input only. It is non-nesting and removes
through the first complete closing tag, including mixed plain/DSML forms.

A possible raw opening saves a scalar checkpoint of the downstream lexer and
thinking state. Its provisional view advances incrementally until it mismatches
(commit the literal view) or completes an opening (suppress its payload). A
complete closing tag discards that block by restoring the checkpoint. Thus the
second pass sees text joined across removed blocks, while the first pass never
recursively consumes its own output. The second pass permanently suppresses a
complete orphan opening; its EOF-only truncated-opening view remains provisional.

Malformed-prefix removal sends text only to the thinking reader, never backward
to orphan recognition. The checkpoint contains scalar/string references, not
copies of historical reasoning sets. Valid tool-tag prefixes cannot complete a
thinking delimiter; on mismatch their downstream work is committed, so completed
thinking parts and independently arriving channel-reasoning updates never need
rollback. The differential matrix covers both directions of this distinction.

## Work model

Token ingestion supplies the authoritative delta. Ordinary append processing is
linear in new UTF-16 input units, not the accumulated answer length. Thinking
lookahead is bounded by delimiter length. XML whitespace is retained for exact
literal recovery but recognized by state transitions, not repeated prefix regexes.
Reasoning-channel producers supply before/after identities and deltas so live and
durable merges do not repeatedly scan the channel accumulator.

The parser exposes input/step counters for source-executing tests. They measure
semantic work, not every string allocation, browser layout, serialization, scene
projection, or total rendering cost. Explicit recovery and terminal reconciliation
are linear in the retained prefix and are measured separately from token ingestion.

## Preserved owners and deferred work

The existing prose/reasoning scheduler, fade cursors, reconnect disposal, anchor
registry, rendering, outcome hydration, terminal/Steer settlement, and scroll
ownership remain upstream-owned. No old paint scheduler, DOM-reuse implementation,
projection cache, or transport-generation fence is introduced here.

Dirty projection is described separately in
[incremental activity projection](incremental-activity-projection.md).
Transport-generation ownership remains a later stage. Passing these parser tests
alone is not evidence that either later stage is implemented.

## Regression evidence

- `test_pr7478_stage1_semantics.py`: durable/live grammar, every-prefix and chunked
  parity with the retained batch oracle, tool-sealing order, recovery channel seed,
  post-tool raw boundaries, ordered replacement composition, mixed/adjacent blocks,
  long DSML whitespace, seeded adversarial combinations, and channel merge work.
- `test_pr7478_residual_semantic_cost.py`: repeated publication and spaced reads
  cannot rescan a growing plain-text prefix.
- `test_pr7478_replay_prefix.py`: real page/attachment/localStorage recovery with
  deterministic event delivery and time, including inline and malformed XML input.
  This proves page behavior, not provider/network or stale-browser callback timing.
- Existing reasoning-owner, fade, terminal, anchor, viewport and scroll regressions
  remain applicable. The batch grammar is retained for settled rendering and as an
  independent semantic oracle, not replaced by a test reimplementation.
