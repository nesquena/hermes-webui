# Decisions

Record important decisions with the reason and consequence. Do not store every
small preference here.

## 2026-04-23 - Centralize Chae-Min Instructions

Decision: use `HERMES.md` as the centralized project instruction file for
Chae-Min.

Reason: Hermes prioritizes `HERMES.md`/`.hermes.md` for project context, and
duplicated persona files make behavior harder to maintain.

Consequence: `KEI_PERSONA.md` was removed after its contents were merged into
`HERMES.md`.

Related: [[projects]] [[kei-jarvis]]

## 2026-04-23 - Use Layered Memory

Decision: keep `USER.md` and `MEMORY.md` compact, and store larger context in
`knowledge/`.

Reason: Hermes built-in memory has small character limits and should not become
a database.

Consequence: `MEMORY.md` acts as a router to this knowledge base.

Related: [[memory-architecture]] [[index]] [[workflows]]

## 2026-04-23 - Use Obsidian Wikilinks As Soft KG

Decision: use Obsidian-compatible Markdown and `[[wikilinks]]` as the default
soft knowledge graph format for Chae-Min notes.

Reason: Kei wants the knowledge base to grow gradually into a graph without
requiring a full RAG or KG system immediately.

Consequence: new knowledge notes should include meaningful `Related:` links and
stable kebab-case link names.

Related: [[memory-architecture]] [[sources]] [[projects]] [[workflows]]

## 2026-04-23 - Use Codex As Maintainer

Decision: use Codex as the preferred maintainer for Chae-Min's operating system,
instruction files, knowledge scaffold, verification scripts, and structural
repairs. Chae-Min may call the configured Codex skill for this work.

Reason: Chae-Min should focus on daily work and should not repeatedly rewrite
its own identity or operating rules during normal use. Codex is better suited to
repo edits, diffs, and verification.

Consequence: persona drift fixes should go through the maintenance workflow,
with the smallest necessary patch and a decision record for changes to identity,
autonomy, security, memory, or knowledge architecture.

Related: [[maintenance]] [[memory-architecture]] [[security-guardrails]]

## 2026-04-23 - Refactor HERMES Into Compact Constitution

Decision: shrink `HERMES.md` into a compact operating contract and move deeper
protocols into `knowledge/` notes.

Reason: a large instruction file can make Chae-Min cautious, rule-bound, and
less adaptive. The better growth path is compact authority plus knowledge,
skills, verification, and maintenance loops.

Consequence: `HERMES.md` now carries the essential identity, priorities,
autonomy, memory routing, Codex maintenance, and hard rules. Detailed research
protocol lives in [[research]] and other deeper protocols remain in their
relevant knowledge notes.

Related: [[maintenance]] [[research]] [[chamin]] [[memory-architecture]]

## 2026-04-23 - Create Chae-Min Starter Skills

Decision: create focused Chae-Min skills for research briefs and maintenance
audits.

Reason: Hermes improves through procedural memory, but Chae-Min had mostly been
using instructions and memory rather than explicit skills. Focused skills make
research and self-maintenance load on demand instead of bloating `HERMES.md`.

Consequence: Chae-Min should use `/chamin-research-brief` for research-heavy
work and `/chamin-maintenance-audit` for persona drift, memory bloat, missing
skills, or operating-system repair.

Related: [[research]] [[maintenance]] [[chamin]]
