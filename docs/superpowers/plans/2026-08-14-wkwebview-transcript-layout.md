# Bounded Transcript Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make oversized historical payloads safe to render in WKWebView without changing canonical transcript data or requiring Swift transcript workarounds.

**Architecture:** A pure JavaScript display-projection helper will recognize expensive opaque/base64 runs and bound only the string passed into the existing render pipeline. `S.messages`, API payloads, recovery state, and action handlers retain the original value. Existing media rendering remains authoritative for supported image data URIs.

**Tech Stack:** Vanilla JavaScript, Node.js executable test harnesses, pytest, Swift/WKWebView for final integration verification.

## Global Constraints

- Do not mutate API responses, `S.messages`, persisted recovery state, or server transcript data.
- Do not globally truncate ordinary user or assistant prose.
- Preserve supported image data URI behavior from the existing media renderer.
- Add no dependency, framework, build step, or transcript-specific native recovery behavior.
- The final acceptance test uses a Swift build without the fetch-response truncation interceptor.

---

### Task 1: Pure display projection

**Files:**
- Modify: `static/ui.js` near the media policy constants and transcript render loop
- Create: `tests/test_transcript_display_projection.py`

**Interfaces:**
- Consumes: original transcript text as `string` plus `{surface: string}`.
- Produces: `_projectTranscriptTextForDisplay(value, options): string`, which returns a bounded display string without mutating `value` or its owning message.

- [ ] **Step 1: Write the failing executable test**

Create a Node-driven pytest harness that extracts the production helper and asserts observable values:

```python
def test_opaque_payload_is_bounded_without_mutating_source(driver_path):
    payload = "prefix data:application/octet-stream;base64," + ("A" * 200_000)
    result = project(driver_path, payload, surface="tool")
    assert result["source"] == payload
    assert len(result["display"]) < 70_000
    assert "abbreviated for display" in result["display"]

def test_ordinary_prose_and_supported_media_are_unchanged(driver_path):
    prose = "A normal paragraph. " * 5_000
    assert project(driver_path, prose, surface="message")["display"] == prose
    image = "data:image/png;base64,iVBORw0KGgo="
    assert project(driver_path, image, surface="message")["display"] == image
```

The driver must pass JSON through stdin, invoke the real helper extracted from
`static/ui.js`, and return both the source and display strings so mutation is
tested directly.

- [ ] **Step 2: Run the new test and prove RED**

Run:

```bash
./scripts/test.sh tests/test_transcript_display_projection.py -v
```

Expected: FAIL because `_projectTranscriptTextForDisplay` is absent.

- [ ] **Step 3: Implement the minimal pure helper**

Add a display-only policy with explicit limits and a bounded replacement for
long opaque runs:

```javascript
const _TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT=60000;
const _TRANSCRIPT_DISPLAY_NOTICE='[opaque payload abbreviated for display]';

function _projectTranscriptTextForDisplay(value, options={}){
  const text=String(value||'');
  const surface=String(options.surface||'message');
  if(_isSafeDataImageUri(text)) return text;
  const opaque=/data:(?:application|image)\/[a-z0-9.+-]+(?:;[a-z0-9=.+-]+)*;base64,[a-z0-9+/=\r\n]+/ig;
  return text.replace(opaque, match=>{
    if(_isSafeDataImageUri(match)) return match;
    if(match.length<=_TRANSCRIPT_DISPLAY_OPAQUE_RUN_LIMIT) return match;
    const head=match.slice(0,2048);
    return `${head}\n\n${_TRANSCRIPT_DISPLAY_NOTICE} (${match.length} characters; ${surface})`;
  });
}
```

Keep the helper pure: no writes to the message object, DOM, localStorage, or
global recovery state.

- [ ] **Step 4: Integrate at the shared transcript display seam**

Immediately before `_getCachedRender`, derive projected display values while
retaining the existing canonical locals for action metadata:

```javascript
const projectedDisplayContent=_projectTranscriptTextForDisplay(
  displayContent,
  {surface:isUser?'user':'assistant'}
);
let bodyHtml=_getCachedRender(projectedDisplayContent,isUser);
```

Apply the same helper to reasoning/tool-detail text immediately before those
values enter HTML/`textContent`. Do not assign projected values back to
`m.content`, `m.reasoning`, `m.reasoning_content`, tool snippets, or `S.messages`.

- [ ] **Step 5: Run focused tests and prove GREEN**

Run:

```bash
./scripts/test.sh \
  tests/test_transcript_display_projection.py \
  tests/test_data_uri_images.py \
  tests/test_tool_card_preview_summary.py \
  tests/test_inflight_storage_quota.py -v
```

Expected: all tests PASS.

- [ ] **Step 6: Commit the independently testable WebUI fix**

```bash
git add static/ui.js tests/test_transcript_display_projection.py
git commit -m "fix: bound opaque transcript payload rendering"
```

### Task 2: Neighboring regression verification

**Files:**
- Modify only if a test exposes a direct regression in Task 1's files.
- Test: existing `tests/test_*render*.py`, `tests/test_*tool*.py`, and recovery tests.

**Interfaces:**
- Consumes: `_projectTranscriptTextForDisplay(value, options)` from Task 1.
- Produces: verification evidence that media, tools, recovery, and transcript rendering remain compatible.

- [ ] **Step 1: Run the JavaScript lint guard**

```bash
npm run lint:runtime
```

Expected: PASS with no new runtime-global or syntax error.

- [ ] **Step 2: Run neighboring UI tests**

```bash
./scripts/test.sh \
  tests/test_data_uri_images.py \
  tests/test_tool_card_preview_summary.py \
  tests/test_ui_tool_call_cleanup.py \
  tests/test_live_to_final_anchor_visible_order.py \
  tests/test_inflight_storage_quota.py \
  tests/test_transcript_display_projection.py
```

Expected: all tests PASS.

- [ ] **Step 3: Review the complete branch diff**

```bash
git diff --check fork/master...HEAD
git diff --stat fork/master...HEAD
git status --short
```

Expected: only the approved design/plan, `static/ui.js`, and its regression test are changed; the worktree is clean after commits.

### Task 3: Remove native workaround and validate integration

**Files:**
- Modify in `/Users/keith/src/hermes-swift-mac`: `Sources/HermesAgent/BrowserWindowController.swift`
- Test in `/Users/keith/src/hermes-swift-mac`: existing Swift test suite and rebuilt app

**Interfaces:**
- Consumes: WebUI branch from Tasks 1-2 served to the native application.
- Produces: proof that no Swift API-response transcript mutation is required.

- [ ] **Step 1: Remove only the native fetch-response interceptor**

Retain `window.__HERMES_NATIVE_MAC__ = true;` if the WebUI uses it for native
shell behavior, but delete the `window.fetch` wrapper that clones `/api/session`
responses and recursively truncates strings. Do not add replacement transcript
logic to Swift.

- [ ] **Step 2: Run Swift verification and rebuild**

```bash
swift test
./build.sh
```

Expected: all Swift tests PASS and the app builds successfully.

- [ ] **Step 3: Install and exercise the unmodified transcript shell**

Replace `/Applications/Hermes Agent.app` with the rebuilt app using the existing
project installation procedure. Against a server running the WebUI branch,
open session `f2e2e88a92c5`, switch to `eda3296f3be0`, switch back, and confirm
click/navigation responsiveness without sustained WebContent CPU saturation.

- [ ] **Step 4: Commit the Swift cleanup separately**

```bash
git add Sources/HermesAgent/BrowserWindowController.swift
git commit -m "fix: rely on WebUI transcript rendering bounds"
git push origin fix/wkwebview-loading-conversation
```

### Task 4: Final WebUI publication

**Files:**
- No additional source files expected.

**Interfaces:**
- Consumes: verified commits from Tasks 1-3.
- Produces: pushed WebUI branch `fix/wkwebview-transcript-layout` on `fork`.

- [ ] **Step 1: Run final verification from a clean worktree**

```bash
git status --short
npm run lint:runtime
./scripts/test.sh tests/test_transcript_display_projection.py tests/test_data_uri_images.py tests/test_tool_card_preview_summary.py tests/test_inflight_storage_quota.py
```

Expected: clean status and all commands PASS.

- [ ] **Step 2: Push the WebUI branch**

```bash
git push -u fork fix/wkwebview-transcript-layout
```

- [ ] **Step 3: Report exact publication and validation evidence**

Report the WebUI branch/commit, Swift cleanup branch/commit, test counts, and
manual session-switch result. Explicitly identify anything that could not be
verified against the deployed WebUI server.
