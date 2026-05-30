# Project OS harness drift vs. product bug triage

Use this note when a Project OS extension test fails, the control-plane surface shows a timeout/stale-running state, or a seeded workflow board looks wrong. The goal is to separate two very different failure classes:

- harness drift: the test/doc/browser harness is no longer describing the current intended implementation
- product bug: the shipped Project OS behavior is wrong even when the harness is reading it correctly

This document is intentionally tied to the source-level regression coverage in `tests/test_project_os_extension_regressions.py` and the runtime truth in `extensions/project-os/project-os-extension.js`.

## Quick rule

Treat the browser/runtime session as the primary truth first, then ask whether the test is still asserting that truth.

- If browser behavior, source strings, and intended semantics still agree but a test or manual checklist says otherwise, suspect harness drift.
- If the browser/runtime surface contradicts the intended semantics below and the source-level tests either fail or are missing, suspect a product bug.

## Required evidence before classifying

For every triage pass, collect all three evidence lanes:

1. Source test evidence
   - `tests/test_project_os_extension_regressions.py`
   - Identify the exact test name that matches the symptom.
2. Runtime/source evidence
   - `extensions/project-os/project-os-extension.js`
   - Check the relevant lifecycle branch, string literal, or seed definition.
3. Browser/loop evidence
   - Open the Project OS surface and the dedicated project session.
   - Record what the user-facing status chip/detail/host note actually says.
   - If the symptom is timing-sensitive, re-check after a fresh `refreshProjectSession()`-equivalent browser poll instead of trusting one stale paint.

Do not classify from only one lane.

## Symptom map

### 1) "false timeout" or host timeout banner confusion

Expected semantics:

- The host surface timing out is not, by itself, proof that the Project OS dedicated session timed out.
- `updateSubmitLifecycle()` explicitly keeps the dedicated project session as the authority.
- A visible host timeout banner may only add the note: `A timeout banner is visible on the host surface, but Project OS is using its own session.`

Source anchors:

- `tests/test_project_os_extension_regressions.py::test_project_os_host_timeout_banner_is_not_project_session_truth`
- `tests/test_project_os_extension_regressions.py::test_project_os_submit_lifecycle_does_not_timeout_while_project_session_is_running`
- `extensions/project-os/project-os-extension.js` in `updateSubmitLifecycle()`

Interpretation:

- Harness drift if the test or checklist assumes "host timeout visible" automatically means Project OS failed, while the dedicated session still has `active_stream_id` or pending-user-message truth.
- Product bug if the extension marks the submit state `timed_out` while the dedicated project session still reports active running truth after a fresh session refresh.

### 2) "Stale running" / "Project may be stuck"

Expected semantics:

- `stalled_running` is a special state, not a generic timeout.
- It means the dedicated project session still looks live (`active_stream_id` or pending user message), but `message_count` is still zero long enough to be suspicious.
- The current threshold is `RUNNING_STALLED_MS = 45 * 1000`.

Source anchors:

- `tests/test_project_os_extension_regressions.py::test_project_os_submit_lifecycle_marks_zero_message_active_stream_as_stalled_running`
- `extensions/project-os/project-os-extension.js::getProjectSessionRunningState(...)`
- `extensions/project-os/project-os-extension.js::restoreProjectSessionContinuity()`

Interpretation:

- Harness drift if a test or doc collapses `stalled_running` into plain `timed_out` even though the runtime still exposes active-stream truth and the status label remains `Project may be stuck`.
- Product bug if the UI never escalates to `stalled_running` for a zero-message active stream after the stall window, or if it shows `stalled_running` after visible reply growth proves the session is healthy.

### 3) Root-seed / root-card semantics

Expected semantics:

- The workflow root seed is a reference-only continuity anchor.
- It must be created as `done`, not as the next actionable triage card.
- Runtime payloads such as assignee/workspace should attach to ready seed cards, not the root anchor.

Source anchors:

- `tests/test_project_os_extension_regressions.py::test_project_os_workflow_root_seed_is_reference_only_done_anchor`
- `tests/test_project_os_extension_regressions.py::test_project_os_workflow_runtime_payload_only_assigns_ready_seed_cards`
- `extensions/project-os/project-os-extension.js` `PROJECT_WORKFLOW_SEEDS`
- `extensions/project-os/project-os-extension.js` `getWorkflowSeedRuntimePayload(seed)`

Interpretation:

- Harness drift if a manual checklist or older continuity note still expects the root seed to appear as actionable work.
- Product bug if the actual seeded board creates the root card in a ready/todo state, or if runtime payload fields land on the root card instead of the next ready slice.

## Triage procedure

### A. Start with the failing assertion or user-visible symptom

Write down one concrete claim, for example:

- "Project OS timed out"
- "Project may be stuck"
- "Root seed became actionable"

If you cannot phrase the failure as one concrete claim, you are not ready to classify it.

### B. Find the matching source-level test

Open `tests/test_project_os_extension_regressions.py` and match the claim to the nearest named test.

The test names are part of the triage surface. If there is no matching test, that is a coverage gap, not evidence of a product bug.

### C. Check runtime truth in the extension source

Read the corresponding branch in `extensions/project-os/project-os-extension.js`.

Questions to answer:

- Is the current code still intentionally expressing the same rule as the test?
- Did a literal string, threshold, or state-name change without the test/docs moving with it?
- Is the code clearly preferring dedicated-session truth over host-surface noise?

If code and intended behavior agree but the test wording does not, classify as harness drift.

### D. Confirm with browser/loop evidence

Use the live surface to answer these specific questions:

- Does the dedicated project session show `active_stream_id`, pending-user-message, or new assistant output?
- Is the visible state chip `Waiting`, `Project may be stuck`, or `Project needs check`?
- Does opening the dedicated session resolve the apparent timeout/stall?
- Is the root seed shown as done/reference-only, or is the UI treating it like the next work item?

Prefer evidence captured immediately after a fresh session reload/open. A stale panel snapshot is not enough.

### E. Classify

Classify as harness drift when any of the following are true:

- the test/checklist assumes host timeout equals dedicated-session timeout
- the test/checklist expects root seed to be actionable
- the test/checklist collapses `waiting`, `stalled_running`, and `timed_out` into one bucket
- the browser/runtime/source all agree, but the harness still asserts an older rule

Classify as product bug when any of the following are true:

- dedicated-session truth is healthy, but the extension still reports `timed_out`
- zero-message active-stream behavior never surfaces `stalled_running`
- new visible replies arrive, but the extension never resolves out of waiting/stalled state
- seeding produces a root card that is actionable instead of reference-only done

## Reporting template

Use this shape in the PR/task note/comment:

```text
Symptom:
Classification: harness drift | product bug
Source test:
Runtime source anchor:
Browser evidence:
Why this is not the other class:
Follow-up:
```

Example "harness drift":

```text
Symptom: Host timeout banner appeared during Project OS import.
Classification: harness drift
Source test: test_project_os_host_timeout_banner_is_not_project_session_truth
Runtime source anchor: updateSubmitLifecycle() keeps dedicated-session truth authoritative and only adds a host-note.
Browser evidence: dedicated project session still had active stream truth; opening the session showed the run continuing.
Why this is not the other class: no premature timed_out transition happened in the Project OS submit state.
Follow-up: update the manual checklist/browser script to stop treating host timeout as terminal.
```

Example "product bug":

```text
Symptom: Root seed card was created as ready and picked up as the next work item.
Classification: product bug
Source test: test_project_os_workflow_root_seed_is_reference_only_done_anchor
Runtime source anchor: PROJECT_WORKFLOW_SEEDS should mark root as done/reference-only.
Browser evidence: seeded board showed the root card in an actionable lane and downstream flow selected it.
Why this is not the other class: the harness expectation matches the intended source contract; runtime behavior violated it.
Follow-up: fix seed creation/runtime payload logic and keep the test pinned.
```

## When to update docs/tests

Update the harness/docs together when the intended rule changes. Do not "fix" this class of failure by editing only the test or only the manual note.

- If the product semantics changed intentionally, update:
  - `tests/test_project_os_extension_regressions.py`
  - this document
  - any relevant manual testing/troubleshooting reference
- If the semantics did not change, fix the runtime bug and keep the docs/tests as-is except for clearer wording.

## Minimal verification command

For doc-linked regression coverage, run:

```bash
pytest tests/test_project_os_extension_regressions.py tests/test_project_os_harness_triage_docs.py -q
```