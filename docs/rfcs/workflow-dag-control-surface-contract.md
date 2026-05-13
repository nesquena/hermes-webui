# Workflow DAG Control Surface API/UX Contract

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task after Hermes workflow API contract approval.

**Status:** Draft v0.1

**Related PRD:** `docs/prd/workflow-dag-control-surface.md`

**Core dependency:** Hermes Agent `docs/specs/hermes-workflow-plugin-architecture.md`

**Goal:** Define the WebUI contract for rendering Hermes workflows as a Level 2 DAG control surface without making the WebUI the source of truth for workflow state.

**Architecture:** WebUI consumes normalized workflow JSON from Hermes Core. React Flow renders the graph, Dagre/ELK computes layout, and a node drawer presents deterministic facts, artifacts, gates, and optional insights. MVP is visibility-first and read-only.

**Tech Stack:** Python WebUI backend routes/proxies, static frontend app, React Flow or compatible graph component once available in the WebUI frontend stack, Dagre or ELK layout library, existing WebUI tests.

---

## 1. Scope

Included:

- WebUI route/API expectations
- normalized response shapes consumed by UI
- DAG visualization UX contract
- node drawer content contract
- facts-vs-insights display rules
- loading/empty/error behavior
- implementation slices and tests

Not included:

- Hermes workflow backend implementation
- graph editing
- direct runtime actions in MVP
- client-side orchestration decisions
- Kanban task body parsing

## 2. Source-of-Truth Contract

The WebUI is a client.

It must not own or infer:

- workflow status
- DAG topology
- node readiness
- dependency validity
- gate requirements
- approval legality
- Kanban mappings
- worktree allocation

The WebUI may cache responses for responsiveness, but must treat Hermes workflow API responses as authoritative.

Rule:

> If a fact is not present in the workflow API response, the WebUI should display it as unavailable, not reconstruct it from prose.

## 3. Expected Hermes API

The WebUI expects Hermes to expose read-only workflow endpoints.

Canonical backend endpoints:

```text
GET /api/workflows
GET /api/workflows/{workflow_id}
GET /api/workflows/{workflow_id}/dag
GET /api/workflows/{workflow_id}/nodes/{node_id}
GET /api/workflows/{workflow_id}/events
GET /api/workflows/{workflow_id}/artifacts
```

The WebUI may proxy these through its own backend to handle active profile, configured Hermes URL, auth, CORS, and user-facing error normalization.

Recommended WebUI proxy routes:

```text
GET /api/workflows
GET /api/workflows/{workflow_id}
GET /api/workflows/{workflow_id}/dag
GET /api/workflows/{workflow_id}/nodes/{node_id}
GET /api/workflows/{workflow_id}/events
GET /api/workflows/{workflow_id}/artifacts
```

The route names intentionally mirror Hermes Core unless existing WebUI routing requires a prefix such as `/api/hermes-workflows`.

## 4. List Response Contract

`GET /api/workflows`

```json
{
  "workflows": [
    {
      "id": "wf_123",
      "title": "Workflow system MVP",
      "description": "Build read-only workflow DAG visibility.",
      "status": "running",
      "scale": "large",
      "board": "default",
      "workspace_path": "/mnt/c/Users/colebienek/pepchat",
      "current_gate": "dag_approved",
      "counts": {
        "nodes": 8,
        "waiting": 2,
        "ready": 1,
        "running": 2,
        "blocked": 0,
        "done": 3
      },
      "updated_at": "2026-05-12T20:00:00Z",
      "created_at": "2026-05-12T19:30:00Z"
    }
  ]
}
```

Display requirements:

- Empty state if list is empty.
- Show title, status, scale, board, updated time, and node counts.
- If API unavailable, show workflow feature unavailable with the backend error and a retry action.

## 5. Workflow Detail Contract

`GET /api/workflows/{workflow_id}`

```json
{
  "workflow": {
    "id": "wf_123",
    "title": "Workflow system MVP",
    "description": "Build read-only workflow DAG visibility.",
    "status": "running",
    "scale": "large",
    "board": "default",
    "workspace_path": "/mnt/c/Users/colebienek/pepchat",
    "policy": {
      "path": ".hermes/workflow.yaml",
      "snapshot_hash": "sha256:..."
    },
    "current_gate": {
      "id": "gate_123",
      "type": "implementation_review",
      "level": 1,
      "status": "pending"
    },
    "facts": {
      "nodes_total": 8,
      "nodes_done": 3,
      "nodes_blocked": 0,
      "critical_path_node_ids": ["spec", "backend-api", "integration"]
    },
    "insights": {
      "summary": "Optional LLM-written summary, if provided by Hermes."
    }
  }
}
```

Display requirements:

- Facts section is authoritative.
- Insights section is optional and labeled as non-authoritative synthesis.
- Missing `insights` should not be an error.

## 6. DAG Response Contract

`GET /api/workflows/{workflow_id}/dag`

```json
{
  "workflow_id": "wf_123",
  "layout_hint": "elk",
  "nodes": [
    {
      "id": "backend-api",
      "title": "Implement backend workflow API",
      "role": "engineer",
      "profile": "engineer",
      "status": "running",
      "gate": {
        "type": "implementation",
        "level": 1,
        "status": "pending"
      },
      "kanban_task_id": "task_abc",
      "branch": "workflow/wf_123/backend-api",
      "worktree_path": ".worktrees/wf_123-backend-api",
      "parents": ["spec-review"],
      "children": ["integration"],
      "badges": ["worktree", "tests"],
      "facts": {
        "started_at": "2026-05-12T20:05:00Z",
        "updated_at": "2026-05-12T20:18:00Z"
      }
    }
  ],
  "edges": [
    {
      "id": "spec-review--backend-api",
      "source": "spec-review",
      "target": "backend-api",
      "kind": "depends_on"
    }
  ],
  "facts": {
    "acyclic": true,
    "node_count": 8,
    "edge_count": 9
  }
}
```

Display requirements:

- Render every node and edge returned.
- Do not add inferred edges.
- If `layout_hint` is absent, default to `elk` or the configured local default.
- If layout computation fails, fall back to a simple layered/topological layout and show a non-blocking warning.

## 7. Node Detail Contract

`GET /api/workflows/{workflow_id}/nodes/{node_id}`

```json
{
  "node": {
    "id": "backend-api",
    "workflow_id": "wf_123",
    "title": "Implement backend workflow API",
    "role": "engineer",
    "profile": "engineer",
    "status": "running",
    "parents": ["spec-review"],
    "children": ["integration"],
    "scope": {
      "summary": "Implement workflow read-only API endpoints.",
      "non_goals": ["Graph editing", "Autonomous replanning"]
    },
    "definition_of_done": [
      "API returns workflow list/detail/DAG/node/events payloads.",
      "Tests cover happy path and missing workflow."
    ],
    "execution": {
      "kanban_task_id": "task_abc",
      "worker_profile": "engineer",
      "claim_id": "claim_123",
      "branch": "workflow/wf_123/backend-api",
      "worktree_path": ".worktrees/wf_123-backend-api",
      "base_ref": "origin/main",
      "started_at": "2026-05-12T20:05:00Z",
      "finished_at": null
    },
    "gate": {
      "id": "gate_456",
      "type": "implementation_review",
      "level": 1,
      "status": "pending",
      "required_actor": "llm_profile",
      "verdict": null,
      "artifact_id": null
    },
    "evidence": {
      "changed_files": [],
      "test_results": [],
      "handoff_artifact_id": null,
      "review_artifact_id": null,
      "pull_requests": [],
      "ci": []
    },
    "artifacts": [
      {
        "id": "artifact_123",
        "kind": "handoff",
        "path": "nodes/backend-api/handoff.yaml",
        "sha256": "...",
        "created_at": "2026-05-12T20:20:00Z"
      }
    ],
    "events": [
      {
        "id": "evt_123",
        "event_type": "node_status_changed",
        "actor_type": "system",
        "actor_id": "workflow-plugin",
        "message": "Node moved to running.",
        "created_at": "2026-05-12T20:05:00Z"
      }
    ],
    "insights": {
      "summary": "Optional LLM node status summary."
    }
  }
}
```

Drawer sections:

1. Summary
2. Scope
3. Definition of Done
4. Execution
5. Gate
6. Evidence
7. Artifacts
8. Audit Events
9. Optional Insights

## 8. Events Contract

`GET /api/workflows/{workflow_id}/events`

```json
{
  "events": [
    {
      "id": "evt_123",
      "workflow_id": "wf_123",
      "node_id": "backend-api",
      "event_type": "node_status_changed",
      "actor_type": "system",
      "actor_id": "workflow-plugin",
      "message": "Node moved to running.",
      "data": {},
      "created_at": "2026-05-12T20:05:00Z"
    }
  ],
  "pagination": {
    "limit": 100,
    "cursor": null,
    "next_cursor": null
  }
}
```

MVP may show only latest events in the node drawer and optionally a workflow-level event timeline.

## 9. Artifacts Contract

`GET /api/workflows/{workflow_id}/artifacts`

```json
{
  "artifacts": [
    {
      "id": "artifact_123",
      "kind": "dag",
      "path": "dag.yaml",
      "sha256": "...",
      "status": "active",
      "created_by": "decomposer",
      "created_at": "2026-05-12T20:00:00Z",
      "url": "/api/workflows/wf_123/artifacts/artifact_123/content"
    }
  ]
}
```

MVP can render artifact metadata and links. Inline artifact viewing is optional.

## 10. Visual Semantics

Status palette:

```text
waiting: gray
ready: blue
running: yellow / amber
blocked: red
failed: red
review: purple
publish: teal
done: green
cancelled: muted gray
```

Node content:

- title
- role/profile
- status indicator
- gate marker if gate exists
- small badges for worktree, PR, tests, blocker, review

Edges:

- directed arrows from prerequisite to dependent node
- muted style for completed upstream edges
- highlighted style for critical path if provided

## 11. Layout Contract

Preferred MVP layout:

1. Convert Hermes nodes/edges to React Flow elements.
2. Apply ELK if installed/configured.
3. Fallback to Dagre if ELK unavailable.
4. Fallback to simple topological columns if both fail.

Layout must be deterministic for the same DAG response to reduce visual jitter.

Do not store layout as workflow truth. If the UI later supports manual layout, persist it as UI preferences, not as DAG topology.

## 12. Page/Navigation Model

Recommended routes:

```text
/workflows
/workflows/:workflowId
```

If the current WebUI navigation prefers a single workspace page, the feature can begin as a panel/tab:

```text
Tasks / Workflows
  - Kanban tasks
  - Workflow DAGs
```

MVP entry point should not require a hidden URL.

## 13. Empty, Loading, and Error States

### Loading

- Show skeleton graph/card placeholders.
- Avoid flashing empty state before first request completes.

### Empty

Message:

```text
No workflows yet.
Workflow DAGs will appear here after Hermes workflow plugin creates or imports one.
```

### Hermes API unavailable

Message should include:

- which backend URL failed if safe to show
- HTTP status/error class
- retry button
- link or hint to gateway/dashboard status if available

### Unsupported backend

If Hermes does not expose workflow capabilities:

```text
Workflow API is not available on this Hermes backend.
```

This is not a frontend crash.

### Partial data

If DAG loads but node detail fails:

- keep graph visible
- show drawer error for that node
- allow retry

## 14. Facts vs Insights UI Rule

Every section that contains LLM-generated text must be labeled `Insights` or `LLM insight`.

Authoritative sections:

- status
- DAG topology
- gates
- artifacts
- test facts
- PR/CI facts
- audit events

Non-authoritative sections:

- summaries
- risk analysis
- next-step recommendations
- semantic interpretation

If the API accidentally returns insight-like text inside `facts`, the UI should still render by schema, but the Core contract should be corrected.

## 15. Refresh Strategy

MVP can poll.

Suggested polling:

- workflow list: every 15–30 seconds while page visible
- selected workflow DAG: every 5–10 seconds while active
- node drawer: refresh when opened and then every 10 seconds while open

Future:

- SSE/WebSocket workflow event stream
- incremental DAG updates

Avoid aggressive polling if the tab is hidden.

## 16. Implementation Plan

### Task 1: Add WebUI workflow API client/proxy skeleton

**Objective:** Provide a single internal client for workflow endpoints.

**Files:**

- Create: `api/workflows.py` or add small route module matching existing backend routing style.
- Test: `tests/test_workflow_routes.py`

**Verification:** missing Hermes workflow backend returns structured unavailable JSON, not HTML shell.

### Task 2: Add workflow list page/entry point

**Objective:** Show workflow list cards/table from `/api/workflows`.

**Files:**

- Modify/create frontend route for `/workflows`.
- Add navigation entry where appropriate.
- Test: route smoke test if frontend tests support it.

**Verification:** empty state, loading state, unavailable state render.

### Task 3: Add DAG data adapter

**Objective:** Convert DAG API response to graph rendering model.

**Files:**

- Create: workflow DAG adapter module in frontend source.
- Test: unit test for nodes/edges conversion.

**Verification:** adapter preserves IDs and does not infer edges.

### Task 4: Add layout helper

**Objective:** Produce deterministic x/y positions via ELK/Dagre/fallback.

**Files:**

- Create: graph layout helper.
- Test: unit test for stable positions and fallback behavior.

**Verification:** same input yields same layout.

### Task 5: Add read-only DAG view

**Objective:** Render graph with status styling and clickable nodes.

**Files:**

- Create: workflow DAG view component.
- Create: node component/status legend.

**Verification:** sample DAG renders all nodes/edges and status classes.

### Task 6: Add node detail drawer

**Objective:** Fetch and render node detail sections.

**Files:**

- Create: node drawer component.
- Test: drawer rendering for complete and partial node payloads.

**Verification:** drawer separates Summary, Scope, DoD, Execution, Gate, Evidence, Artifacts, Audit, Insights.

### Task 7: Add refresh behavior

**Objective:** Poll list/DAG/drawer while visible without excessive requests.

**Files:**

- Add polling hook or reuse existing data refresh pattern.
- Test: fake timer/unit test if available.

**Verification:** no polling when page hidden if visibility helper exists; drawer refresh stops when closed.

### Task 8: Add capability gate

**Objective:** Detect unsupported Hermes backend and show a stable message.

**Files:**

- Modify gateway/status capability logic if needed.
- Test: unsupported workflow API returns visible disabled state.

**Verification:** feature never falls through to HTML or unhandled JSON parse errors.

## 17. Testing Strategy

Backend/proxy tests:

```bash
python -m pytest tests/test_workflow_routes.py -q
```

Broader WebUI regression tests:

```bash
python -m pytest tests/test_sprint*.py tests/test_*workflow*.py -q
```

If the frontend has JS tests, add focused adapter/layout tests there. If not, keep data-shape tests in Python and add browser/manual verification notes until the frontend test harness exists.

Manual verification:

1. Start Hermes backend exposing workflow API.
2. Start WebUI.
3. Open `/workflows`.
4. Confirm workflow list renders.
5. Open a workflow.
6. Confirm DAG auto-layout and status styling.
7. Click each node state type.
8. Confirm drawer sections render.
9. Stop Hermes workflow API and confirm graceful unavailable state.

## 18. Acceptance Criteria

MVP WebUI is acceptable when:

- A user can navigate to workflow DAGs without a hidden route.
- Workflow list loads from normalized Hermes API data.
- Selected workflow renders every returned DAG node and edge.
- Node status is visible at a glance.
- Clicking a node opens a detail drawer.
- Drawer shows scope, DoD, execution, gate, evidence, artifacts, audit events, and optional insights.
- UI never parses Kanban task prose to infer graph topology.
- UI labels LLM-generated summaries as insights.
- Loading, empty, unsupported, API error, and partial node-detail failures are handled gracefully.
- The feature is read-only unless Core mutation endpoints are explicitly implemented.

## 19. Open Questions

1. Does this WebUI currently use React components, static HTML modules, or a custom rendering layer for the relevant screens? The implementation plan should bind to the actual frontend architecture before code begins.
2. Should workflow routes live at `/workflows`, inside an existing Tasks page, or behind a feature flag first?
3. Should the WebUI proxy Hermes workflow APIs or call Hermes directly from browser once CORS/capabilities are stable?
4. Which layout engine should be the first dependency: ELK for quality or Dagre for smaller footprint?
5. Should artifact content be viewable in the drawer for MVP, or just linked?
