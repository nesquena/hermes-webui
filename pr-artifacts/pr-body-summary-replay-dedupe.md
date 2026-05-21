## Summary

Follow-up to #2651. That PR fixed one replay boundary, but continued testing exposed the same context-invariant violation at additional WebUI merge/metering boundaries.

This PR makes the replay protection context-engine agnostic:

- strips replayed non-adjacent context blocks and near-duplicate large Session Arc Summary cards before writing model context
- applies the same replay guard to the non-streaming `/api/chat` writeback path
- treats `context_messages` as the authoritative model-facing prefix when reconciling sidecar state with `state.db`, appending only demonstrably new state rows
- caps live tool-result prompt estimates so the context ring does not treat large in-flight tool outputs as exact prompt growth

LCM/continuation made these failures easy to reproduce, but the invariant is broader than LCM:

> If `context_messages` exists, it is the authoritative model-facing prefix. `messages`/`state.db` may be fuller or noisier histories and should only contribute true deltas. Live usage estimates must not override exact prompt accounting.

## Why this happens

WebUI currently merges several histories that have different meanings:

- `context_messages`: compact model-facing context for the next call
- `messages`: visible display transcript
- `state.db`: append-only runtime/session journal, including tool rows

After compression/continuation, those sources can overlap. The old code sometimes treated append candidates as wholly new:

```text
clean context_messages + whole state.db transcript
```

or:

```text
previous_context + replayed_tail + new_delta
```

That reintroduced old summaries, tool rows, or active-tail messages into the next model context or into the live usage estimate.

## Failure cases covered

The detailed debugging artifact is in `pr-artifacts/context-replay-failure-cases.md`. The key cases are:

1. **Compression continuation replays the active tail**
   - `result_messages` can contain `previous_context + replayed_tail + new_delta`.
   - Prefix slicing alone saves the replayed tail again.

2. **Near-duplicate large Session Arc Summary cards**
   - Large `[Session Arc Summary ...]` messages can share a huge prefix while differing in refreshed tails/hints.
   - Exact-match dedupe misses them.

3. **Non-adjacent replay blocks**
   - Replayed blocks can be separated by compression markers/summaries/tool rows, so adjacent-only dedupe is insufficient.

4. **Non-streaming `/api/chat` writeback missed the replay guard**
   - The streaming path deduped context writeback; synchronous chat restored reasoning metadata and saved directly.

5. **Turn-start state reconciliation polluted a clean sidecar context**
   - With `prefer_context=True`, a clean sidecar context could still be followed by mirrored `state.db` transcript rows.
   - The next runtime prompt grew even though persisted `context_messages` stayed compact.

6. **Live metering over-counted large in-flight tool results**
   - Tool callbacks can arrive before exact next-prompt accounting.
   - The old live estimate added full rough tool-result tokens to `last_prompt_tokens`, causing context-ring jumps that disappeared after cancel/persisted refresh.

## Implementation notes

- `_dedupe_replayed_context_messages(...)` now handles non-adjacent replay blocks and large near-duplicate summary cards.
- `/api/chat` writeback calls the same context replay guard as streaming writeback.
- `state_db_delta_after_context(...)` uses `context_messages` as the authoritative prefix and only returns state rows after the last state row already represented by sidecar context.
- `_bounded_live_tool_prompt_delta(...)` bounds live-only tool estimate growth while preserving exact compressor/provider prompt accounting when available.

## Test plan

```bash
python -m pytest -q tests/test_streaming_live_usage_estimate.py tests/test_issue1217_transcript_compaction.py tests/test_session_save_mode.py
git diff --check
python -m compileall -q api/models.py api/streaming.py api/routes.py
```

Current local result:

```text
43 passed
```
