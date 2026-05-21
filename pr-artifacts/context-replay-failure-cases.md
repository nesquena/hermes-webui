# Context replay / progress-ring failure cases

This artifact records concrete failure cases observed while debugging WebUI + LCM context replay. It is intended to support an upstream PR with reproducible rationale.

## A. Compression continuation replays active tail into display/context

Observed shape after compression/continuation:

```text
previous_context = [summary, A, B, C]
result_messages = previous_context + [A, B, C, D]
old saved context/display = [summary, A, B, C, A, B, C, D]
expected = [summary, A, B, C, D]
```

Code-level cause: writeback assumed `result_messages[len(previous_context):]` was all new delta. After LCM/session rollover, the agent may replay the active tail after the compacted prefix, so this assumption is false.

Regression target: strip candidate prefixes that are already suffixes of existing context/display.

## B. Near-duplicate large Session Arc Summary cards

Observed in WebUI sidecar display transcript for sessions in the `20260520_200424_a43cef` / `20260520_201320_a95eac` lineage:

```text
[Session Arc Summary (d1, node 39)] ... 62k chars
[Session Arc Summary (d1, node 39)] ... 62k chars
[Session Arc Summary (d1, node 39)] ... 74k chars
```

These summaries shared thousands of identical prefix characters but differed in tails/expand hints. Exact identity checks missed them. One duplicated ~80k char summary explains a ~20k-token jump.

Regression target: treat large `[Session Arc Summary ...]` messages with the same long prefix as replayed summary artifacts.

## C. Non-adjacent replay blocks separated by markers/summaries

Observed display transcript contained repeated blocks that were not immediately adjacent, e.g. best block lengths around 171 messages in historical `messages`:

```text
A B C ... [compression marker / summary / unrelated rows] ... A B C
```

Adjacent-only dedupe falsely reported clean. This matters because LCM/continuation can insert compression cards, cron banners, or summary messages between original block and replayed tail.

Regression target: detect and strip replayed non-adjacent blocks when appending model context candidates.

## D. Non-streaming `/api/chat` writeback missed dedupe

Observed session: `20260521_060755_294aed`.

User asked a short question with no meaningful new tool usage:

```text
这是一个内部服务对么？简答
```

Before cleanup:

```text
context_messages: 136
best replay: 67 messages repeated from index 0 at index 67
last_prompt_tokens: 136668 (~53.4% of 256k)
```

Expected shape was:

```text
previous_context(67) + new_user + new_assistant = 69 messages
```

Actual cause: streaming writeback used `_dedupe_replayed_context_messages`, but synchronous `/api/chat` wrote `_restore_reasoning_metadata(previous_context, result_messages)` directly to `s.context_messages`.

Regression target: both streaming and non-streaming writeback paths must use the same replay-dedupe guard.

## E. Runtime/progress-ring jump: clean persisted context, polluted turn-start reconciliation

Observed session: `20260521_060755_294aed` after cleanup and deployment.

Persisted sidecar after pause/cancel:

```text
context_messages: 69
context chars: 199,972
rough content tokens: ~49,993
last_prompt_tokens: 86,723 (~33.9%)
```

But starting/continuing a streaming turn made the progress ring jump to ~55%. Simulating the turn-start code path showed:

```text
ctx_before_agent: 154 messages
chars: 448,438
rough content tokens: ~112,109
```

After applying existing final-writeback dedupe to that runtime prompt:

```text
after_current_dedupe: 85 messages
chars: 248,466
rough content tokens: ~62,116
```

So the ring was not randomly wrong: it reflected a polluted runtime prompt estimate. The persisted sidecar stayed clean because final writeback/cancel did not save the runtime replay.

Code-level cause: streaming turn start does:

```python
_previous_context_messages = _new_turn_context_from_messages(
    reconciled_state_db_messages_for_session(
        s,
        prefer_context=True,
        state_messages=_external_state_messages,
    ),
    msg_text,
)
```

When `prefer_context=True`, sidecar `context_messages` are clean, but `state.db` still contains mirrored/replayed transcript rows. `reconciled_state_db_messages_for_session` append-only merges `context_messages + whole state transcript`, so the agent/runtime prompt temporarily receives old transcript rows again.

Regression target: when `prefer_context=True` and sidecar `context_messages` exists, reconciliation must return:

```text
clean sidecar context + truly newer state.db delta
```

not:

```text
clean sidecar context + full state.db transcript
```

## PR thesis

The bug family is not a model behavior issue. It is a WebUI persistence/reconciliation invariant violation:

> Model-facing context is append-only, but append candidates may contain replayed context due to LCM/session continuation/state-db mirroring. Every boundary that merges result/state messages into model context must strip replayed prefixes/blocks and must distinguish clean sidecar context from full display/state transcripts.

Key invariant for upstream:

```text
If context_messages exists, it is the authoritative model-facing prefix.
State/db/display histories may be fuller/noisier and should only contribute messages that are demonstrably newer than that prefix.
```


## F. Live metering over-counts large in-flight tool results after cancel/retry

Observed after deploying the reconciliation fix and retrying `continue` in session `20260521_060755_294aed`:

```text
persisted sidecar context: 69 messages, ~49,993 rough content tokens
state.db messages: 90 messages
state delta after context: 21 messages, ~21,226 rough content tokens
turn-start context after fix: 90 messages, ~71,219 rough content tokens
last_prompt_tokens persisted: 86,723 (~33.9%)
```

The previous full-transcript turn-start replay was gone, but the ring still jumped during the run. The new jump came from live metering, not persisted context reconciliation. The run executed several large `read_file` tool calls (5k / 17k / 13k chars). `_record_live_tool_complete()` fed each bounded preview into `_bump_live_prompt_estimate()`, which added the full rough tool-result tokens to `last_prompt_tokens` before any exact next-prompt accounting was available. Repeated cancel/retry makes this look like context replay even when final sidecar context remains clean.

Regression target: live tool metering should be a conservative UI hint and must not inflate `last_prompt_tokens` by the full content of large in-flight tool results. Exact provider/compressor prompt accounting should still win when available.
