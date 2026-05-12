# Source Trail — agentmemory

Checked: 2026-05-12 00:41 JST
Source: https://github.com/rohitg00/agentmemory
Status: recon; do not install into Yuto core yet

## Conclusion

`agentmemory` is a local-first persistent memory runtime for AI coding agents. It is relevant to Yuto because it tries to solve the same class of problem Yuto is building manually: cross-session memory, hook-based capture, MCP tools, retrieval, compression, knowledge graph, and multi-agent shared memory.

Recommendation:

```text
Borrow ideas and run an isolated sandbox later; do not replace Yuto's Markdown KG / CocoIndex / Hermes memory yet.
```

Best fit for Yuto:

- reference architecture for hook-based memory capture;
- possible future sandbox for cross-agent coding memory;
- ideas for memory lifecycle: working / episodic / semantic / procedural;
- ideas for audit, forget, retention, and retrieval evals;
- cautionary security lessons for any automatic memory capture system.

## Verified repo facts

From GitHub API on 2026-05-12 JST:

- Repo: `rohitg00/agentmemory`
- URL: https://github.com/rohitg00/agentmemory
- Description: `#1 Persistent memory for AI coding agents based on real-world benchmarks`
- Created: 2026-02-25
- Updated: 2026-05-11
- Last pushed: 2026-05-11
- Stars observed: 4,419
- Forks observed: 415
- Open issues observed: 37
- License: Apache-2.0
- Default branch: `main`
- Primary language: TypeScript
- npm package name from `package.json`: `@agentmemory/agentmemory`
- npm package version in repo: `0.9.8`
- Node engine: `>=20.0.0`

Root files/dirs observed:

- `README.md`
- `package.json`
- `AGENTS.md`
- `ROADMAP.md`
- `SECURITY.md`
- `GOVERNANCE.md`
- `benchmark/`
- `integrations/`
- `packages/`
- `plugin/`
- `src/`
- `test/`
- `.github/security-advisories/`

## What it claims to be

README positioning:

- persistent memory for AI coding agents;
- works with agents that support hooks, MCP, or REST API;
- shared memory server across Claude Code, Cursor, Gemini CLI, Codex CLI, OpenCode, Hermes, and other MCP clients;
- built on `iii-engine` / `iii-sdk`;
- local-first, no external DB required by default.

The README reports:

- 95.2% retrieval R@5 on LongMemEval-S with BM25+Vector;
- 86.2% R@5 BM25-only fallback;
- 51 MCP tools in the main badge, while the Hermes integration README mentions 43 MCP tools. Treat tool counts as version-sensitive and verify before setup.

## Architecture observed

`AGENTS.md` says the architecture is built on `iii-engine` primitives:

```text
Worker / Function / Trigger
```

Important architecture facts from source inspection:

- Engine: `iii-sdk` / iii-engine, WebSocket to port 49134.
- State: file-based SQLite through iii-engine StateModule at `./data/state_store.db`.
- Build: TypeScript to ESM via `tsdown`, output to `dist/`.
- Tests: `vitest`; `npm test` excludes integration tests.
- Main package: `@agentmemory/agentmemory`, CLI binary `agentmemory`.
- Dependencies include `iii-sdk`, `zod`, Anthropic SDK packages, optional local embedding dependencies `@xenova/transformers` and `onnxruntime-*`.

Source files show memory functions for:

- observe/capture;
- search;
- remember;
- privacy redaction;
- graph retrieval;
- context injection;
- timeline;
- relations;
- retention;
- auto-forget;
- export/import;
- Obsidian export;
- team/mesh/signals/leases;
- eval/validator/self-correct.

## Memory model

README describes a hook-based pipeline:

```text
hook captures event
-> privacy filter
-> raw observation
-> LLM compression into structured facts/concepts/narrative
-> vector embedding
-> BM25 + vector + graph indexing
-> session start context injection
```

README also describes 4 memory tiers:

```text
Working = raw observations
Episodic = compressed session summaries
Semantic = extracted facts and patterns
Procedural = workflows and decision patterns
```

This maps well to Yuto's current memory architecture:

```text
active Hermes memory = compact pointers
Markdown KG = durable truth
CocoIndex = derived index/cache
skills/playbooks = procedural memory
session_search = older transcripts
```

## Benchmarks observed

`benchmark/LONGMEMEVAL.md` reports:

- Dataset: LongMemEval-S, 500 questions, about 48 sessions per question, about 115K tokens each.
- Metric: recall_any@K.
- Embedding model: `all-MiniLM-L6-v2`, local, 384 dimensions.
- No LLM in loop for benchmark; pure retrieval evaluation.
- Reported results:
  - BM25+Vector R@5 95.2%, R@10 98.6%, R@20 99.4%, NDCG@10 87.9%, MRR 88.2%.
  - BM25-only R@5 86.2%, R@10 94.6%.

`benchmark/COMPARISON.md` includes an apples-vs-oranges caveat: agentmemory/MemPalace LongMemEval-S results are not directly comparable to Letta/Mem0 LoCoMo results.

Yuto relevance:

- Useful as a retrieval benchmark reference.
- Do not treat the benchmark as proof that agentmemory is better for Yuto's legal/forensic KG without local pilot.
- LongMemEval evaluates retrieval, not legal/forensic correctness, privacy safety, or memory governance.

## Hermes integration observed

`integrations/hermes/README.md` provides two options:

1. MCP server via `~/.hermes/config.yaml`:

```yaml
mcp_servers:
  agentmemory:
    command: npx
    args: ["-y", "@agentmemory/mcp"]

memory:
  provider: agentmemory
```

2. Deeper Hermes plugin copied into `~/.hermes/plugins/agentmemory` for pre-LLM context injection, turn-level capture, MEMORY.md mirroring, and system prompt block injection.

Yuto caution:

- This touches Hermes Agent configuration and memory provider behavior.
- Any setup must load the `hermes-agent` skill first.
- Do not install or configure in Yuto core without explicit approval, backup, and sandbox plan.
- Current Yuto already has a working Markdown KG + CocoIndex path; replacement would create overlap and risk.

## Security findings and cautions

The repo includes `.github/security-advisories/` with past/advisory drafts. Observed examples:

- stored XSS in real-time viewer;
- `curl | sh` remote code execution issue;
- REST/stream services binding to `0.0.0.0` by default in affected versions `<0.8.2`;
- unauthenticated mesh sync in affected versions `<0.8.2`;
- incomplete secret redaction in affected versions `<0.8.2`.

Important lesson for Yuto:

Automatic memory capture is dangerous if secrets, tool outputs, HTTP headers, prompts, or raw case data enter the memory store.

Source inspection of `src/functions/privacy.ts` shows redaction patterns for private tags and common token formats, but regex redaction is never a complete privacy guarantee.

Yuto-specific constraints:

- Never use this on real victim evidence or secrets by default.
- Bind only to localhost.
- Require `AGENTMEMORY_SECRET` if any REST/API surface is exposed.
- Disable mesh/federation unless explicitly needed and authenticated.
- Treat viewer and export endpoints as sensitive.
- Avoid MEMORY.md mirroring unless reviewed; Yuto keeps active memory intentionally small.

## Fit with Yuto

### What to borrow

1. Hook-based capture pattern for coding-agent receipts.
2. Memory lifecycle tiers: working / episodic / semantic / procedural.
3. Retrieval eval discipline using held-out queries.
4. Audit/forget/retention as first-class functions.
5. Local embedding fallback pattern.
6. MCP as optional cross-agent access layer.
7. Viewer/replay ideas for inspecting what got captured.
8. Stronger memory poisoning and secret-leak threat model.

### What not to adopt yet

1. Do not replace Markdown KG as source of truth.
2. Do not route Yuto active memory to agentmemory.
3. Do not auto-capture all Hermes/tool output into persistent memory.
4. Do not ingest legal/forensic real case data.
5. Do not enable mesh/shared memory.
6. Do not use `curl | sh` setup patterns.
7. Do not assume privacy redaction catches all secrets.

### Possible sandbox pilot

Use only synthetic/non-sensitive data:

```text
sandbox repo / synthetic coding session
-> run agentmemory locally on 127.0.0.1
-> set AGENTMEMORY_SECRET
-> disable mesh
-> capture coding-agent events only
-> test memory_recall against known tasks
-> export data and inspect for secrets/noise
-> compare against Yuto session_search + Markdown KG + CocoIndex
```

Pilot questions:

1. Does it reduce Kei/Yuto re-explaining across coding sessions?
2. Does it retrieve better than `session_search` + CocoIndex for coding tasks?
3. Does auto-capture create too much noise?
4. Can it avoid storing secrets and sensitive data?
5. Can exported memory be curated back into Markdown KG safely?
6. Does it conflict with Hermes memory/provider behavior?

## Decision

For Yuto today:

```text
Borrow concepts; sandbox later; do not install into core yet.
```

For the AI harm evidence company team:

- agentmemory is useful for Engineering / Product Systems and Knowledge Infrastructure, especially coding-agent memory and receipts.
- It is not suitable as the primary store for legal/forensic evidence.
- It should not replace the evidence-first Markdown KG and CocoIndex architecture.

## Next action

If Kei wants to proceed:

1. create an isolated sandbox under `/Users/kei/kei-jarvis/tools/agentmemory_sandbox/`;
2. use synthetic coding-agent sessions only;
3. verify localhost binding and `AGENTMEMORY_SECRET`;
4. test MCP recall with Hermes only after loading `hermes-agent` skill and backing up config;
5. write a short pilot report before any core adoption.
