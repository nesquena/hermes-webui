# Workflow DAG Control Surface PRD

## Status

Draft v0.1 companion PRD for the Hermes Workflow System.

## Relationship to Hermes Core

The WebUI is a client/control surface for workflow state owned by Hermes Core. It must not become the source of truth for workflow state, DAG topology, approval rules, or task orchestration.

Hermes Core workflow capability/plugin owns:

- workflow state
- DAG topology
- node status
- artifact references
- approval gates
- audit events
- Kanban task mapping
- worktree metadata
- validation and materialization rules

The WebUI owns:

- visual representation
- operator navigation
- node inspection
- approval/review surfaces
- display of deterministic facts and optional LLM insights
- future operator actions mediated through Hermes APIs

## Goal

Provide a Level 2 graphical DAG experience for Hermes workflows: a user can see a workflow graph, understand runtime state at a glance, click into any node, and inspect useful operational details without digging through logs or raw Kanban task text.

## MVP scope

MVP should include:

1. Workflow list or entry point.
2. Read-only DAG visualization for a selected workflow.
3. React Flow rendering.
4. Dagre or ELK auto-layout.
5. Status-aware node styling.
6. Clickable nodes.
7. Node detail drawer.
8. Gate/status visibility.
9. Deterministic fact display.
10. Clearly labeled optional LLM insight display if provided by Hermes.
11. Loading, empty, and error states.

Not MVP:

- Graph editing.
- Drag/drop mutation of dependencies.
- Direct graph actions such as retry/reassign/reclaim.
- Auto-replanning UI.
- Client-side orchestration rules.

## User stories

### Operator sees workflow progress

As an operator, I want to open a workflow and immediately see which nodes are waiting, ready, running, blocked, done, or under review so I can understand progress.

### Operator inspects a node

As an operator, I want to click a DAG node and see its scope, definition of done, assignee, worktree/branch, task ID, evidence, handoff, and audit events.

### Operator sees blockers

As an operator, I want blocked nodes to be visually obvious and inspectable so I can determine whether human action is required.

### Operator distinguishes facts from insights

As an operator, I want deterministic workflow facts separated from optional LLM-generated insights so I know what is authoritative.

### Operator sees gates

As an operator, I want approval gates to be visible in the DAG and node drawer so I know whether a workflow is waiting for human review, LLM-auditable review, or an external blocker.

## Data source expectations

The WebUI should consume normalized workflow data from Hermes APIs. It should not infer DAG topology from prose task bodies.

Expected data categories:

- workflows
- workflow detail
- DAG nodes and edges
- node detail
- audit events
- artifact references
- deterministic status summaries
- optional LLM insights

Example API shape, subject to Hermes architecture decisions:

```text
GET /api/workflows
GET /api/workflows/:workflow_id
GET /api/workflows/:workflow_id/dag
GET /api/workflows/:workflow_id/nodes/:node_id
GET /api/workflows/:workflow_id/events
GET /api/workflows/:workflow_id/artifacts
```

## DAG visualization requirements

Use React Flow with Dagre or ELK auto-layout.

The graph should render:

- nodes
- directed dependency edges
- node status color/icon
- node role/profile label
- gate markers where relevant
- blocked/error indicators
- critical path marker if provided by Hermes

Initial status palette suggestion:

- gray: waiting / not ready
- blue: ready
- yellow: running
- red: blocked or failed
- green: done
- purple: review
- teal: publish

Exact design can follow the WebUI design system.

## Node detail drawer

Clicking a node opens a drawer or side panel.

Recommended sections:

### Summary

- title
- node ID
- workflow ID
- role
- assigned profile
- status
- parents
- children
- current gate, if any

### Scope

- task body / node description
- definition of done
- non-goals
- linked PRD/spec/artifact references

### Execution

- Kanban task ID
- worker profile
- worker run/claim ID if available
- worktree path
- branch name
- base branch
- start/finish timestamps

### Evidence

- changed files
- test commands/results
- handoff artifact
- review verdict
- PR links
- CI/check status

### Audit

- state transitions
- approval/rejection events
- blocker reasons
- retry/reclaim history
- actor type: human, LLM profile, system middleware

## Approval visibility

The MVP is visibility-first. It should show gate state clearly even if approval actions are implemented later.

Gate display should indicate:

- gate type: PRD, spec, DAG, implementation review, publish, external
- required approval level: none, LLM-auditable, human breakpoint, external mandatory
- current verdict: pending, approved, rejected, blocked
- actor and timestamp if resolved
- linked review artifact if applicable

## Facts vs insights

Separate deterministic status from LLM-generated summaries.

Suggested UI sections:

- **Facts** — authoritative state from Hermes workflow middleware.
- **Insights** — optional LLM synthesis over the facts.

This prevents an insight summary from being mistaken for source-of-truth state.

## Future runtime actions

Future versions may allow graph operations:

- approve gate
- retry node
- reclaim worker
- reassign node
- block/unblock node
- split node
- add dependency
- open PR
- open logs
- open worktree

All actions must call Hermes workflow APIs and be validated by Hermes before state changes.

## Empty/loading/error states

The UI should handle:

- no workflows yet
- workflow exists but DAG not materialized
- Hermes workflow API unavailable
- Hermes returns validation errors
- node detail unavailable
- partial data, e.g. DAG available but artifacts missing

Error states should help distinguish:

- WebUI connection problem
- Hermes API unavailable
- workflow plugin unavailable
- malformed workflow data
- missing artifact

## Acceptance criteria

1. A user can open a workflow DAG rendered from Hermes-provided normalized data.
2. The DAG uses an automatic layout and preserves directed dependency semantics.
3. Node colors/icons reflect status from Hermes.
4. Clicking a node opens a detail drawer with summary, scope, execution, evidence, and audit sections.
5. Approval gate status is visible.
6. Deterministic facts and optional LLM insights are visually separated.
7. The WebUI does not mutate workflow state in MVP.
8. The WebUI does not infer orchestration rules independently from Hermes.
9. Missing/partial/unavailable workflow data has clear error or empty states.

## Open questions

1. Should live updates use polling, SSE, or websocket?
2. Which route should host workflow DAGs in the WebUI navigation?
3. Should workflows be integrated into existing task/Kanban screens or receive a dedicated Workflows page?
4. What is the exact normalized DAG API shape from Hermes?
5. Should React Flow node layouts be persisted per workflow or recalculated on load?
6. How much raw artifact content should be displayed inline versus linked/opened separately?
