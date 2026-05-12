# Yuto Memory Palace

Created: 2026-05-12 JST
Status: active retrieval map

Purpose: keep active memory small while preserving high-quality recall after details are demoted out of `USER.md` / `MEMORY.md`.

Related: [[memory-system]] [[second-brain-dashboard]] [[source-mempalace]] [[yuto-memory-scout]] [[yuto-multi-book-expert-skill-factory]]

## Principle

Active memory is the hot hallway. The palace is the map of rooms. The source files are the evidence.

```text
USER.md / MEMORY.md -> short pointer
memory-palace.json -> stable room / palace_id / retrieval commands
knowledge + sessions + skills -> source of truth
CocoIndex -> derived search cache
Yuto -> verifies before claiming or promoting
```

## Commands

```bash
cd /Users/kei/kei-jarvis
python tools/second_brain.py palace list
python tools/second_brain.py palace search "latest recall"
python tools/second_brain.py palace search "Book Expert Factory"
python tools/second_brain.py palace doctor
```

## Storage Budget

Kei explicitly allows up to 100 GB for palace-backed durable recall.

This is a budget, not a target to fill. Use it for verified source trails, extracted metadata, receipts, embeddings/indexes, and reproducible caches. Do not use it to bloat `USER.md` / `MEMORY.md`, duplicate long source text unnecessarily, or store unreviewed raw noise as authority.

Default storage lane for palace-specific artifacts:

```text
knowledge/memory-palace/
```

## Storage

Machine-readable palace:

```text
knowledge/memory-palace.json
```

Each entry has:

- `palace_id`
- `wing`
- `room`
- `title`
- `summary`
- `paths`
- `commands`
- `tags`

## Quality Gate

Before demoting an active-memory entry, Yuto should verify:

1. The detail exists in a durable source file, raw session, or skill.
2. A palace entry points to the durable location.
3. `python tools/second_brain.py palace doctor` passes.
4. The active memory replacement is a short pointer, not a mini-summary.
5. A recall canary works, e.g. `python tools/second_brain.py palace search "<topic>"` returns the intended room.

## Current Rooms

- `ke-iprefs-core`: Kei preference anchors and rules pointer.
- `yuto-recall-latest`: latest/raw-session recall protocol.
- `yuto-memory-demotion`: active-memory demotion lane.
- `yuto-memory-scout`: read-only Memory Scout.
- `second-brain-cocoindex`: Markdown truth + CocoIndex cache.
- `book-expert-factory`: book registry and expert blueprint pipeline.
- `systems-thinking-templates`: reusable systems-thinking templates.
- `ai-harm-evidence-company`: Japan-first AI harm evidence company context.
- `palace-storage-budget-100gb`: 100 GB storage budget guardrail for palace-backed durable recall.
- `yuto-codebase-search-toolbelt`: CocoIndex Code + Yuto hybrid search for foreground self-improvement work.

## Non-goals

- The palace is not a second active prompt.
- It is not authority over facts.
- It should not duplicate long source content.
- It should not auto-promote memory or skills.
