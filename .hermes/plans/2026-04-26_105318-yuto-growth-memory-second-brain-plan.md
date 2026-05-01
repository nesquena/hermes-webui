# Yuto Growth Memory + Second Brain Organization Plan

Date: 2026-04-26
Owner: Yuto control plane
Purpose: let Yuto grow autonomously with direction, without turning memory into noisy rule bulk or letting unverified reflections become authority.

## Goal

Design a right-sized information architecture for Yuto's growth:

- active memory stays small and operational;
- knowledge notes hold durable context and doctrine;
- skills hold repeatable procedures;
- session archives and reflection candidates support learning but never become authority directly;
- graph/DB layers make the second brain inspectable and queryable;
- Taeyoon/Jarvis can support maintenance and execution without owning Yuto identity.

## Current verified context

Inspected files:

- `/Users/kei/kei-jarvis/knowledge/index.md`
- `/Users/kei/kei-jarvis/knowledge/memory-system.md`
- `/Users/kei/kei-jarvis/knowledge/yuto-autopilot.md`
- `/Users/kei/kei-jarvis/knowledge/yuto.md`
- `/Users/kei/kei-jarvis/knowledge/yuto-graph-second-brain-plan.md`
- `/Users/kei/kei-jarvis/knowledge/reflection-pipeline.md`
- `/Users/kei/.hermes/memories/MEMORY.md`
- `/Users/kei/.hermes/memories/USER.md`
- `/Users/kei/kei-jarvis/tools/yuto_graph/build_graph.py`
- `/Users/kei/kei-jarvis/tests/test_yuto_graph.py`

Inventory verified 2026-04-26:

```text
/Users/kei/kei-jarvis/knowledge              exists=True  md=28    files=33
/Users/kei/.hermes/skills                    exists=True  md=375   files=705
/Users/kei/jarvis-vault                      exists=True  md=5542  files=5811
/Users/kei/jarvis-intel                      exists=True  md=88    files=3977
/Users/kei/kei-jarvis/conversation-archive/redacted      exists=True  md=1 files=1
/Users/kei/kei-jarvis/conversation-reflections           exists=True  md=1 files=10
```

Graph status verified:

```text
python3 -m tools.yuto_graph.build_graph --root /Users/kei/kei-jarvis/knowledge --out /Users/kei/kei-jarvis/knowledge/.graph
nodes=27 edges=150 broken=12 orphans=0
python3 -m pytest tests/test_yuto_graph.py -q
3 passed
```

DB surfaces verified:

```text
/Users/kei/kei-jarvis/legal-research-center/research.db exists=True, 14 tables/views
/Users/kei/.hermes/state.db exists=True, 10 tables/views including sessions/messages FTS
```

Current issue:

- The architecture already exists in pieces, but the operating direction is scattered.
- `knowledge/.graph/report.md` shows 12 broken links, including `[[completion-contract]]`, `[[memory-architecture]]`, `[[wikilinks]]`, and placeholder examples from `index.md`.
- The graph indexes only Yuto knowledge now, not skills/memory/Jarvis vault.
- Reflection pipeline exists as policy and guard paths, but not an active promotion workflow.
- The LRC operational DB is separate from Yuto memory; it should stay operational/source data, not become identity memory.

## Storage layer map

### 1. Active injected memory

Paths:

- `/Users/kei/.hermes/memories/USER.md`
- `/Users/kei/.hermes/memories/MEMORY.md`

Role:

- `USER.md`: stable Kei preferences and safety expectations.
- `MEMORY.md`: active pointers, high-risk reminders, tool quirks, current project pointers.

Rules:

- No project logs.
- No large research summaries.
- No model inventories as truth.
- Current state claims must be rechecked live.

Growth direction:

- Keep under pressure small; use it as a router, not a database.
- Use canaries after edits.

### 2. Yuto knowledge base

Path:

- `/Users/kei/kei-jarvis/knowledge/`

Role:

- source-of-truth Markdown second brain for durable context, decisions, source trails, self-lessons, architecture, research protocol, and maintenance doctrine.

Growth direction:

- Use Obsidian-compatible Markdown + wikilinks.
- Add short, high-value notes only when they reduce future verification/search cost.
- Prefer one focused note per durable concept.
- Avoid empty notes and decorative graph growth.

### 3. Skills / procedural memory

Path:

- `/Users/kei/.hermes/skills/`

Role:

- repeatable procedures, commands, pitfalls, verification steps.

Growth direction:

- Promote only after repeated real use or repeated failure.
- Patch stale skills immediately when a loaded skill is wrong.
- Avoid creating skills for speculative ideas.

### 4. Raw sessions and reflection candidates

Paths:

- `/Users/kei/.hermes/state.db`
- `/Users/kei/.hermes/sessions/session_*.json`
- `/Users/kei/kei-jarvis/conversation-archive/redacted/`
- `/Users/kei/kei-jarvis/conversation-reflections/{candidate,promoted,rejected,stale}/`

Role:

- raw evidence and candidate learning, not authority.

Growth direction:

- Event-driven reflection after complex work, repeated failures, compression, or session close.
- Local LLM may draft candidate memories, but Yuto must verify before promotion.
- Redaction before reflection.
- Candidate canary before treating a reflection as review-ready.

### 5. Graph index / generated machine map

Paths:

- `/Users/kei/kei-jarvis/tools/yuto_graph/`
- `/Users/kei/kei-jarvis/knowledge/.graph/nodes.json`
- `/Users/kei/kei-jarvis/knowledge/.graph/edges.json`
- `/Users/kei/kei-jarvis/knowledge/.graph/report.md`

Role:

- generated visibility layer over Markdown, not source of truth.

Growth direction:

- Phase 1: improve current Yuto knowledge graph health.
- Phase 2: add skills and active memory as read-only roots.
- Phase 3: add Jarvis vault as read-only external layer.
- Phase 4: optional SQLite graph/semantic retrieval after graph quality improves.

### 6. Operational DBs and artifacts

Paths:

- LRC DB: `/Users/kei/kei-jarvis/legal-research-center/research.db`
- LabOps: `/Users/kei/kei-jarvis/lab-ops/`
- Jarvis vault/intel: `/Users/kei/jarvis-vault/`, `/Users/kei/jarvis-intel/`

Role:

- operational state, queues, article corpus, agent handoffs, outputs.

Growth direction:

- Keep separate from identity memory.
- Summarize durable lessons into knowledge notes only after verification.
- Use DB/live metrics for operational claims.

## Proposed direction: Yuto Growth Loop v1

```text
work / research / failure / user correction
-> evidence capture
-> route to correct layer
-> graph index refresh
-> canary / regression check
-> optional skill update
-> weekly review of growth direction
```

Key principle:

```text
Yuto grows by better routing, retrieval, verification, and skills — not by dumping more text into active memory.
```

## Step-by-step plan

### Phase 0 — Stabilize the map, no architecture rewrite

Scope: read-only / generated files only.

1. Rebuild Yuto knowledge graph.
2. Classify broken links:
   - placeholder examples to ignore or escape;
   - missing useful notes to create;
   - renamed concepts to redirect;
   - skill references that should be represented as skill nodes.
3. Add a graph health note or report section with:
   - node count;
   - edge count;
   - broken links;
   - orphan notes;
   - top missing concepts.

Acceptance:

- broken-link list is categorized, not blindly fixed.
- no source note rewrites except plan/explicit approved hygiene patches.

Verification:

```bash
cd /Users/kei/kei-jarvis
python3 -m tools.yuto_graph.build_graph --root /Users/kei/kei-jarvis/knowledge --out /Users/kei/kei-jarvis/knowledge/.graph
python3 -m pytest tests/test_yuto_graph.py -q
```

### Phase 1 — Add right-sized schema for growth governance

Create a note, not a new bureaucracy:

- `/Users/kei/kei-jarvis/knowledge/yuto-growth-memory-map.md`

It should contain:

- storage layer map;
- promotion rules;
- retrieval rules;
- current graph health;
- weekly review questions;
- escalation rules to Taeyoon/Jarvis.

Acceptance:

- note links from `knowledge/index.md`.
- note says what Yuto should do next when unsure where information belongs.

### Phase 2 — Extend graph index to include active memory + skills read-only

Update `tools/yuto_graph` so the graph can include:

- `/Users/kei/.hermes/memories/USER.md`
- `/Users/kei/.hermes/memories/MEMORY.md`
- `/Users/kei/.hermes/skills/**/SKILL.md`

Design:

- keep generated graph only;
- do not rewrite memory/skills;
- classify `memory` and `skill` node types;
- add edges from skills' `related_skills` metadata where easy.

Acceptance:

- tests cover memory node and skill node classification.
- graph report shows counts by source/layer.

Verification:

```bash
cd /Users/kei/kei-jarvis
python3 -m pytest tests/test_yuto_graph.py -q
python3 -m tools.yuto_graph.build_graph --root /Users/kei/kei-jarvis/knowledge --out /Users/kei/kei-jarvis/knowledge/.graph --extra-root /Users/kei/.hermes/skills
```

### Phase 3 — Reflection pipeline pilot, event-driven only

Run one safe pilot on the current/recent session only if useful:

```bash
cd /Users/kei/kei-jarvis
python3 tools/reflection_pipeline/export_session_candidate.py
python3 tools/reflection_pipeline/candidate_canary.py
```

Rules:

- no local LLM promotion without Yuto review;
- no unredacted secrets;
- no automatic writing to `USER.md`, `MEMORY.md`, or skills;
- candidate memory must include provenance and canary.

Acceptance:

- one candidate is either promoted, rejected, or left candidate with reason.
- active memory does not grow unless a high-value pointer is confirmed.

### Phase 4 — Jarvis/Obsidian external layer read-only

Only after Phase 0-2 are stable:

- index `/Users/kei/jarvis-vault/` as `external_note` source;
- do not write to Jarvis vault;
- sample only if full vault graph is too large;
- represent ownership boundary clearly:
  - Yuto controls Yuto knowledge and final synthesis;
  - Taeyoon/Jarvis provide AgentOps/domain outputs;
  - external notes are evidence/context, not Yuto identity.

Acceptance:

- report distinguishes Yuto-owned vs external notes.
- no automatic promotion from external vault to active memory.

### Phase 5 — Optional SQLite graph / semantic RAG

Only when graph JSON is useful and query needs exceed JSON/Markdown search.

Possible DB:

- `/Users/kei/kei-jarvis/knowledge/.graph/yuto_graph.sqlite`

Tables:

- `nodes(id, type, title, path, source, mtime, hash)`
- `edges(source, target, type, evidence)`
- `diagnostics(kind, item, source, created_at)`

RAG only after:

- graph health is acceptable;
- source provenance is represented;
- retrieval can cite source paths;
- poisoning controls exist.

## Weekly growth review questions

Yuto should answer these weekly, not daily:

1. What did Yuto learn from real work this week?
2. Which learning belongs in USER/MEMORY/knowledge/skill/archive only?
3. Which memory is stale, too broad, or unverified?
4. Which skill was missing or wrong during real work?
5. Which graph links are broken because a useful concept is missing?
6. Which operational metrics show Yuto's system improved, not just more notes?
7. Does Taeyoon/Jarvis need a scoped task for maintenance or AgentOps?

## Safety / boundaries

Do not autonomously:

- delete or move notes;
- rewrite `HERMES.md`, identity, security, or memory architecture;
- write into Jarvis vault or Obsidian vault;
- promote local LLM reflections to memory;
- expose secrets from raw sessions;
- claim growth from graph/report generation alone.

Ask Kei or route to Taeyoon for large architecture changes.

## Immediate next recommended action

Do not start with a big DB/RAG build.

Start with:

1. Create `knowledge/yuto-growth-memory-map.md` from this plan.
2. Patch `knowledge/index.md` to link it.
3. Categorize current 12 broken graph links.
4. Add tests to `tools/yuto_graph` for memory/skill roots.
5. Only then consider graph SQLite.

## Completion Contract for this planning task

Task: Plan Yuto memory / DB / second-brain organization for autonomous growth with direction.
Target metric before: no current consolidated plan file for growth memory map in `.hermes/plans`; existing graph report had 27 nodes, 150 edges, 12 broken links, 0 orphan notes.
Action taken: inspected authority and memory files, verified graph and DB surfaces, wrote this plan.
Target metric after: consolidated plan file exists with verified inventory, storage layer map, phased roadmap, safety boundaries, and verification commands.
Verification command: read this file; run graph build and `pytest tests/test_yuto_graph.py -q`.
Status: closed for planning; implementation remains not started.
If partial, next owner: N/A for planning. Implementation next owner is Yuto, with Taeyoon review if architecture grows beyond read-only/generated graph work.
