# agentmemory Deep Dive

Checked: 2026-05-12 JST
Source: https://github.com/rohitg00/agentmemory
Related: [[source-agentmemory]], [[source-cocoindex]], [[source-mempalace]], [[memory-system]], [[yuto-ai-harm-evidence-company-team-v0.2]], [[security]]
Status: detailed learning note; sandbox-only recommendation

## 1. Executive Verdict

`agentmemory` is a serious local-first memory runtime for AI coding agents. It is not just a small MCP server. The repo contains a broad memory operating system:

```text
hooks -> privacy filter -> observations -> compression -> search/index -> context injection -> recall/tools -> audit/forget/retention
```

It is useful for Yuto as:

1. a reference architecture for automatic coding-agent memory;
2. a benchmark/source of design patterns for Yuto's Knowledge Infrastructure Division;
3. a future sandbox candidate for coding-agent memory across Codex/OpenCode/Hermes;
4. a security warning about how dangerous automatic memory capture can be.

Recommendation:

```text
Learn from it deeply. Do not adopt into Yuto core yet.
Sandbox only, synthetic/non-sensitive data only.
```

Why:

- Yuto already has a safer curated architecture: `Markdown KG = truth`, `CocoIndex = derived index`, `Yuto = verifier/router`.
- `agentmemory` auto-captures broad session/tool data; that is high-risk for secrets and legal/forensic evidence.
- The repo has good security posture now, but its own advisories show the attack surface is real.

## 2. Verified Source Facts

From GitHub/API/source inspection on 2026-05-12 JST:

- Repo: `rohitg00/agentmemory`
- URL: https://github.com/rohitg00/agentmemory
- Description: persistent memory for AI coding agents based on benchmarks
- Created: 2026-02-25
- Updated/pushed: 2026-05-11
- Stars observed: 4,419
- Forks observed: 415
- Open issues observed: 37
- License: Apache-2.0
- Primary language: TypeScript
- npm package: `@agentmemory/agentmemory`
- Repo package version: `0.9.8`
- Node requirement: `>=20.0.0`
- Core dependency: `iii-sdk`
- Optional local embedding dependencies: `@xenova/transformers`, `onnxruntime-node`, `onnxruntime-web`

Root files checked:

- `README.md`
- `package.json`
- `AGENTS.md`
- `ROADMAP.md`
- `SECURITY.md`
- `GOVERNANCE.md`
- `iii-config.yaml`
- `docker-compose.yml`
- `integrations/hermes/README.md`
- `benchmark/*.md`
- `src/functions/*`
- `src/mcp/tools-registry.ts`
- `src/triggers/api.ts`
- `.github/security-advisories/*`

## 3. Product Positioning

README positions it as:

```text
Persistent memory for Claude Code, Cursor, Gemini CLI, Codex CLI, pi, OpenCode, Hermes, and any MCP client.
```

The key promise:

```text
Your coding agent remembers everything. No more re-explaining.
```

Meaning:

- session work is automatically captured;
- important context is compressed and indexed;
- later sessions retrieve only relevant context;
- multiple agents can share one memory server.

It is optimized for coding-agent continuity, not legal/forensic evidence governance.

## 4. Architecture

`AGENTS.md` says the project is built on `iii-engine` primitives:

```text
Worker / Function / Trigger
```

Observed architecture:

```text
AI agent / hooks / MCP client
        ↓
agentmemory CLI/server
        ↓
iii-engine worker runtime
        ↓
functions + triggers
        ↓
StateKV / SQLite via iii-engine StateModule
        ↓
BM25 index + optional vector index + graph retrieval
        ↓
REST API / MCP tools / viewer / context injection
```

Important implementation files:

- `src/index.ts`: registers memory functions and providers.
- `src/config.ts`: feature/env configuration.
- `src/triggers/api.ts`: REST endpoint registration and auth checks.
- `src/mcp/tools-registry.ts`: MCP tool definitions.
- `src/functions/observe.ts`: hook/event capture.
- `src/functions/remember.ts`: explicit semantic memory save/forget.
- `src/functions/search.ts`: search and index rebuild.
- `src/state/hybrid-search.ts`: BM25 + vector + graph search fusion.
- `src/functions/privacy.ts`: redaction filter.
- `src/functions/export-import.ts`: memory export/import.
- `src/functions/mesh.ts`: peer/mesh sync.
- `src/functions/team.ts`: team sharing/feed/profile.
- `src/functions/retention.ts`, `auto-forget.ts`: decay/eviction.
- `src/functions/audit.ts`: audit trails.

## 5. Memory Pipeline

The README describes a pipeline roughly like:

```text
Hook fires
  -> Privacy filter strips secrets/API keys/private tags
  -> Store raw observation
  -> LLM compresses to structured facts + concepts + narrative
  -> Vector embedding
  -> Index in BM25 + vector

Stop / SessionEnd hook
  -> summarize session
  -> optional knowledge graph extraction
  -> optional slot reflection

SessionStart hook
  -> load project profile
  -> hybrid search
  -> token budget context
  -> inject memory into conversation
```

Memory tiers:

| Tier | Meaning | Yuto equivalent |
|---|---|---|
| Working | raw observations/tool events | session/tool receipts |
| Episodic | compressed session summaries | session_search / session summaries |
| Semantic | facts/patterns | Markdown KG source notes |
| Procedural | workflows/decision patterns | Hermes skills / playbooks |

Yuto lesson:

- The 4-tier framing is useful.
- Yuto should keep curated truth in Markdown, but can borrow automatic working/episodic capture for coding-agent sandboxes.

## 6. Hook Capture Surface

Observed hook files under `src/hooks/`:

- `session-start.ts`
- `prompt-submit.ts`
- `pre-tool-use.ts`
- `post-tool-use.ts`
- `post-tool-failure.ts`
- `pre-compact.ts`
- `subagent-start.ts`
- `subagent-stop.ts`
- `task-completed.ts`
- `stop.ts`
- `session-end.ts`
- `notification.ts`
- `sdk-guard.ts`

This is powerful because it can capture context without manual agent calls.

It is risky because tool inputs/outputs can contain:

- secrets;
- file contents;
- private prompts;
- logs;
- API responses;
- legal/forensic evidence;
- untrusted text that can poison future context.

Yuto rule:

```text
Auto-capture is acceptable only in a sandbox or tightly scoped coding workflow, never for raw legal/forensic victim data by default.
```

## 7. Search and Retrieval

Observed retrieval stack:

- BM25 search index;
- optional vector index;
- graph retrieval;
- reciprocal rank fusion / weighting;
- optional reranker;
- query expansion;
- temporal graph.

`src/state/hybrid-search.ts` uses:

```text
BM25 results
+ vector results if embedding provider exists
+ graph results if entities exist
-> merged/reranked results
```

MCP tools include:

- `memory_recall`
- `memory_smart_search`
- `memory_file_history`
- `memory_patterns`
- `memory_timeline`
- `memory_profile`
- `memory_graph_query`
- `memory_verify`
- `memory_lesson_save`
- `memory_lesson_recall`
- `memory_obsidian_export`
- `memory_slot_*`

Extracted from `src/mcp/tools-registry.ts`: 51 MCP tools.

REST API extraction found about 110 `/agentmemory/*` endpoints in `src/triggers/api.ts`.

Yuto lesson:

- Its retrieval system is richer than the current CocoIndex lexical/body search.
- But richer retrieval does not equal safer truth.
- Use it as a benchmark reference, not as the canonical evidence/knowledge store.

## 8. Benchmarks

`benchmark/LONGMEMEVAL.md` reports:

- Dataset: LongMemEval-S, 500 questions, about 48 sessions/question, about 115K tokens each.
- Metric: `recall_any@K`.
- Embedding: `all-MiniLM-L6-v2`, local, 384 dimensions.
- No LLM in loop.

Reported results:

```text
BM25 + Vector: R@5 95.2%, R@10 98.6%, R@20 99.4%, NDCG@10 87.9%, MRR 88.2%
BM25-only:     R@5 86.2%, R@10 94.6%, R@20 98.6%
```

`benchmark/COMPARISON.md` includes a caveat that LongMemEval-S results are not directly comparable to LoCoMo results from systems like Letta/Mem0.

`benchmark/REAL-EMBEDDINGS.md` reports an internal dataset with token savings around 92% versus loading everything.

Interpretation:

- Strong retrieval evidence for conversational/coding memory.
- Not evidence of legal/forensic correctness.
- Not evidence of safe secret handling in all real workflows.
- Not evidence that it should replace Yuto's curated KG.

## 9. MCP and REST Surface

MCP tools extracted from source include 51 tools across categories:

- recall/search/context;
- save/remember/forget;
- file history/patterns/timeline/profile;
- export/import;
- graph/relations;
- consolidate/reflect/slots;
- team/mesh/signals/leases;
- audit/governance/delete;
- snapshots/checkpoints/actions/routines;
- Obsidian export;
- verify/lessons;
- diagnostics/heal.

REST API extraction found about 110 endpoints such as:

- `/agentmemory/observe`
- `/agentmemory/context`
- `/agentmemory/search`
- `/agentmemory/smart-search`
- `/agentmemory/remember`
- `/agentmemory/forget`
- `/agentmemory/export`
- `/agentmemory/import`
- `/agentmemory/audit`
- `/agentmemory/graph/query`
- `/agentmemory/mesh/*`
- `/agentmemory/team/*`
- `/agentmemory/viewer`
- `/agentmemory/obsidian/export`

Yuto security implication:

```text
This is a broad local service. If exposed or unauthenticated, it can leak or poison memory.
```

## 10. Hermes Integration

`integrations/hermes/README.md` describes two options.

### Option 1 — MCP server

Example shown in repo:

```yaml
mcp_servers:
  agentmemory:
    command: npx
    args: ["-y", "@agentmemory/mcp"]

memory:
  provider: agentmemory
```

### Option 2 — Deeper Hermes plugin

Repo says copying `integrations/hermes` into `~/.hermes/plugins/agentmemory` gives deeper integration:

- pre-LLM context injection;
- turn-level capture;
- memory-write mirroring to MEMORY.md;
- system prompt block injection.

Yuto caution:

- This modifies Hermes Agent behavior.
- Any actual setup must load `hermes-agent` skill first.
- Do not use `memory.provider: agentmemory` in Yuto core until tested in sandbox.
- Memory mirroring to `MEMORY.md` conflicts with Kei's compact active-memory policy.

## 11. Security and Privacy Analysis

The repo includes security posture docs:

- `SECURITY.md`
- `.github/security-advisories/01-viewer-xss.md`
- `.github/security-advisories/02-curl-sh-rce.md`
- `.github/security-advisories/03-default-bind-0000.md`
- `.github/security-advisories/04-mesh-unauth.md`
- `.github/security-advisories/05-obsidian-export-traversal.md`
- `.github/security-advisories/06-privacy-redaction-incomplete.md`

Important observed advisories:

1. Stored XSS in viewer.
2. `curl | sh` RCE issue.
3. REST/stream services previously bound to `0.0.0.0` by default in affected versions `<0.8.2`.
4. Unauthenticated mesh sync in affected versions `<0.8.2`.
5. Obsidian export traversal.
6. Incomplete privacy redaction in affected versions `<0.8.2`.

Current config observed:

- `iii-config.yaml` binds REST/streams to `127.0.0.1`.
- `docker-compose.yml` maps ports to `127.0.0.1`.
- `src/triggers/api.ts` has bearer auth helpers using `AGENTMEMORY_SECRET` when configured.
- Some behavior allows no auth if secret unset; exact endpoint protection varies and must be verified before running.

Privacy source observed:

- `src/functions/privacy.ts` strips `<private>...</private>` blocks and regex-matches many token patterns.

Risk judgement:

```text
Privacy regex is a mitigation, not a guarantee.
```

Yuto must assume:

- secrets can still slip through;
- untrusted documents can poison memory;
- export/viewer/API endpoints are sensitive;
- mesh/team sharing expands attack surface.

## 12. Governance / Maintenance Quality

Positive signs:

- `GOVERNANCE.md` exists and describes maintainer process.
- `SECURITY.md` has vulnerability reporting process.
- `ROADMAP.md` lays out Q2 2026 to Q1 2027 themes.
- `AGENTS.md` gives detailed project-specific agent instructions.
- `test/` includes many unit tests across memory, privacy, audit, export/import, graph, MCP, replay, viewer security, etc.
- Security advisories are documented, not hidden.

Cautions:

- Repo is young: created 2026-02-25.
- Version is pre-1.0 (`0.9.8`).
- Broad feature surface creates maintenance and security complexity.
- README badges/tool counts may drift from integration docs; verify live version before setup.

## 13. Comparison to Yuto's Current Architecture

### Yuto Current

```text
HERMES/MEMORY = compact active pointers
Markdown KG = source of truth
CocoIndex = derived index/cache/body search
skills = procedural memory
session_search = older conversations
Yuto = verifier/router
```

Strengths:

- curated and evidence-first;
- low hidden auto-capture risk;
- good for legal/forensic source discipline;
- simple to inspect in Markdown;
- easy to keep active memory compact.

Weaknesses:

- less automatic capture;
- weaker semantic/vector retrieval;
- less cross-agent shared memory automation;
- less replay/viewer tooling;
- limited benchmark/eval harness.

### agentmemory

Strengths:

- automatic capture;
- rich retrieval;
- cross-client MCP/REST;
- hooks/viewer/export;
- benchmark-driven;
- local-first by default;
- audit/forget/retention primitives.

Weaknesses:

- high privacy/security attack surface;
- noisy auto-capture risk;
- memory poisoning risk;
- config/runtime complexity;
- not legal/forensic evidence governance by design.

## 14. Yuto Adoption Strategy

### Do now

Borrow concepts:

- 4-tier memory model;
- retrieval benchmark discipline;
- audit/forget/retention requirements;
- hook-capture threat model;
- local-only binding and secret requirements;
- memory poisoning checks;
- viewer/export risk checklist.

### Do later in sandbox

Run only with synthetic/non-sensitive coding tasks:

```text
/Users/kei/kei-jarvis/tools/agentmemory_sandbox/
```

Sandbox requirements:

- Node >=20 available;
- `AGENTMEMORY_SECRET` set;
- bind to `127.0.0.1` only;
- mesh disabled;
- no real victim/legal/forensic evidence;
- no secrets in test repo;
- export inspection after run;
- compare recall against Yuto `session_search` and CocoIndex;
- document setup and teardown.

### Do not do now

- Do not install into Yuto core config.
- Do not change Hermes memory provider.
- Do not copy plugin into `~/.hermes/plugins/agentmemory`.
- Do not let it mirror to active `MEMORY.md`.
- Do not expose REST/API/viewer to network.
- Do not use with real victim data.
- Do not enable mesh/team federation.

## 15. How It Could Fit the New Company Team

Relevant divisions from [[yuto-ai-harm-evidence-company-team-v0.2]]:

### Engineering / Product Systems Division

Possible use:

- coding-agent continuity;
- local model benchmark receipts;
- prototype history;
- implementation failure/retry memory.

### Knowledge & Learning Infrastructure Division

Possible use:

- compare retrieval methods;
- design audit/forget/retention policy;
- build memory eval suite;
- route useful memories into Markdown KG.

### Compliance / Safety / Expert Network Division

Use as threat model reference:

- secret leakage;
- memory poisoning;
- unauthorized API access;
- XSS/viewer/export risk;
- privacy redaction insufficiency.

Not for:

- primary legal evidence storage;
- real victim case evidence;
- court/forensic chain-of-custody;
- APPI-sensitive raw data.

## 16. Proposed Pilot Design

Goal:

Test whether agentmemory improves coding-agent recall enough to justify a limited integration.

Dataset:

- synthetic coding project;
- 5-10 sessions;
- known decisions, files, bugs, fixes;
- no secrets.

Tasks:

1. Start agentmemory local server on localhost.
2. Capture synthetic coding-agent work.
3. Query with MCP/REST memory recall.
4. Export memory and inspect raw stored observations.
5. Compare against:
   - Hermes `session_search`;
   - Yuto Markdown notes;
   - CocoIndex search.
6. Score:
   - recall quality;
   - precision/noise;
   - token use;
   - secret/noise leakage;
   - setup complexity;
   - whether memories can be curated into Markdown KG.

Pass criteria:

```text
No sensitive leakage in export.
Recall beats current workflow on at least 3/5 test questions.
Setup does not require unsafe Hermes config changes.
Memory remains local and authenticated.
Yuto can curate useful items into Markdown KG.
```

Fail criteria:

```text
Captures too much noisy/private data.
Requires broad Hermes memory provider replacement.
REST/viewer/API not safely locked down.
Recall benefit is marginal vs session_search/CocoIndex.
```

## 17. Research Questions for Yuto

1. Can automatic coding-memory capture be limited to safe event types only?
2. Can Yuto use agentmemory as a worker-memory backend without touching core memory?
3. Can exported agentmemory observations become candidate receipts for Yuto Team Lanes?
4. Can we reproduce the LongMemEval-like benchmark on Yuto's own synthetic tasks?
5. Can CocoIndex add vector search and evals without adopting agentmemory runtime?
6. Can agentmemory's audit/forget/retention model inform Yuto's memory policy?
7. What is the minimum safe memory capture surface for Codex/local worker sessions?

## 18. Final Decision

Use agentmemory as a strong learning source and sandbox candidate.

Do not adopt it as the Yuto memory core now.

Best current posture:

```text
Yuto curated KG remains truth.
CocoIndex remains derived search/cache.
agentmemory becomes a studied reference and possible coding-agent memory sandbox.
```

Reason:

- Yuto's legal/forensic direction needs curated, auditable, source-backed knowledge.
- agentmemory is optimized for coding-agent memory continuity.
- The overlap is useful, but the risk profile is different.

## 19. One-Line Takeaway

`agentmemory` is a powerful memory runtime for coding agents, but for Yuto it should be studied and sandboxed—not trusted with core legal/forensic memory until privacy, poisoning, config, and retrieval-eval risks are proven safe.
