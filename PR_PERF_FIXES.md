# Performance: high CPU/memory during agent streaming

## Problem

When the agent is actively responding (SSE stream active), the WebUI consumes
15–30% CPU and 400–500 MB in the browser even on idle polling ticks.

## Root cause

Three independent CPU sinks compound during active streaming:

1. **`startStreamingPoll()` fires every 5s regardless of stream state.**
   SSE already pushes session-attribute updates in real time. When `S.busy` or
   `S.activeStreamId` is set, `renderSessionList()` does unnecessary work:
   fetches updated session data, diffs against in-memory snapshots, and writes
   `innerHTML` into the sidebar DOM — all redundant while the live stream is
   already the authoritative source.

2. **`startGatewayPollFallback()` ignores `document.hidden` and stream state.**
   Same pattern: every N seconds it runs `renderSessionList()` even when the
   tab is backgrounded (Chrome still runs `setInterval` at reduced rate) or
   when a stream is in progress.

3. **`_flushPendingSegmentRender()` falls back to `innerHTML = renderMd()`**
   when the `_smdParser` is unavailable. The incremental `_smdWrite()` path
   appends parsed Markdown to existing DOM without full re-parse. The
   `innerHTML` fallback re-parses the entire growing message body on every
   flush — O(n²) DOM churn over the lifetime of a long answer.

## Changes

| File | Change | Impact |
|------|--------|--------|
| `static/sessions.js` | `startStreamingPoll()` skips tick when `S.busy` or `S.activeStreamId` | Eliminates ~5s DOM renders during active stream |
| `static/sessions.js` | `startGatewayPollFallback()` skips tick when tab hidden or stream active | Eliminates background tab CPU waste |
| `static/messages.js` | `_flushPendingSegmentRender()` recreates `_smdParser` from `window.smd` before falling back to `innerHTML` | Keeps incremental render path for long responses |
