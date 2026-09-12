# Bounded transcript layout design

## Problem

Opening a session can make WKWebView's content process spend all of its time in
synchronous line layout when historical messages contain very large opaque
strings, such as screenshot/base64 payloads embedded in tool results. A native
response interceptor avoids the freeze by truncating the API response, but it
also replaces the browser's canonical message model. That can make edit,
regenerate, copy, fork, export, and later persistence operate on incomplete
content.

## Goal

Sessions with oversized historical fields must open and switch responsively in
WKWebView without any transcript-specific Swift behavior. The WebUI must retain
the complete API response and canonical `S.messages` values.

## Design

Add a shared display-projection helper at the transcript rendering boundary.
It will bound only strings that would otherwise become expensive DOM text,
with a visible notice that the displayed value is abbreviated. Canonical
message objects, API responses, storage, editing, copying/exporting, and model
context remain unchanged.

The projection will distinguish opaque payloads from ordinary prose. Supported
image data URIs continue through the existing media renderer. Oversized tool
output, reasoning/debug payloads, and unsupported opaque/base64 values receive
the bounded display representation. Normal chat prose is not globally capped.
All historical and recovery rendering paths must use the shared helper rather
than adding native-client or per-call-site response mutation.

Current in-flight recovery compaction remains unchanged. Its persisted state is
already bounded and is needed for reload recovery; deleting it would trade the
layout bug for lost live-turn recovery.

## State invariants

- `S.messages` owns the authoritative full transcript and is never mutated by
  display projection.
- DOM text owns the bounded display representation and can be rebuilt from the
  authoritative transcript at any time.
- Edit, regenerate, copy/export, fork, persistence, and model-context paths read
  authoritative data, not projected DOM text.
- Existing valid media rendering remains functional.

## Verification

Add Node-executed regression coverage against the real JavaScript helper. The
test must fail on the base revision and prove that:

1. an oversized opaque historical value produces bounded DOM-facing text;
2. the original nested message value remains byte-for-byte unchanged;
3. ordinary prose and supported media values retain their existing behavior;
4. repeated rendering is deterministic and does not mutate recovery state.

Run the focused transcript, tool-card, data-URI, and in-flight recovery tests,
then the broader JavaScript/UI test neighborhood. Finally, serve the fixed
WebUI and open/switch the reported sessions using a Swift build with the fetch
interceptor removed.

## Scope

No new framework or dependency is introduced. No cache deletion, API response
mutation, server transcript mutation, or transcript-specific native recovery
logic is part of this change.
