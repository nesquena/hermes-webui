# Source Trail — CocoIndex

Checked: 2026-05-10 JST
Source: https://github.com/cocoindex-io/cocoindex
Docs: https://cocoindex.io/docs

## What it is

CocoIndex is an open-source Python/Rust framework for incremental indexing and live context pipelines for AI agents and LLM applications.

Its core model is:

```text
target_state = transformation(source_state)
```

Instead of writing manual delta/update/delete logic for RAG or knowledge indexes, users declare what target state should exist from current source state. CocoIndex stores internal state and applies only the necessary changes when source data or pipeline logic changes.

## Verified source facts

From GitHub API and repo files checked during recon:

- Repo: `cocoindex-io/cocoindex`
- Description: `Incremental engine for long horizon agents`
- License: Apache-2.0
- Language: Python with Rust core
- GitHub metadata at check time: ~9.3k stars, ~695 forks, 54 open issues
- Latest release observed: `v1.0.3`, published 2026-05-05
- Latest main commit observed: `f83e22e3d7b1`, 2026-05-09, `fix(security): validate SQL identifiers in postgres/sqlite connectors (#1947)`
- PyPI package: `cocoindex`, version `1.0.3`
- PyPI classifier: `Development Status :: 3 - Alpha`
- Python requirement from `pyproject.toml`: `>=3.11`
- Local availability checked: `cocoindex_importable=False`, `cocoindex_cli=None`

## Key concepts from docs

### App

Top-level runnable pipeline. It binds a main function and parameters. Running `app.update()` or `cocoindex update main.py` executes the root component, mounts child components, compares target states, and applies only necessary changes.

### Source

A data source such as local files, Google Drive, S3, Postgres, Kafka, etc.

### Processing Component

A stable unit of work, commonly one file/row/entity. Component paths identify the same item across runs. Stable paths are essential because CocoIndex uses them to detect removed/changed items and clean up target states.

### Target State

Declared output that should exist in an external system, e.g. rows in a DB, vector rows, graph nodes, or output files. Target state should be a pure function of source state, not uncontrolled side effects.

### Function memoization

`@coco.fn(memo=True)` skips expensive work when function input and code are unchanged. Useful for embeddings, LLM extraction, PDF conversion, etc.

### Live mode

`cocoindex update main.py -L` or `app.update(live=True)` keeps compatible sources watching/streaming after catch-up. `localfs.walk_dir(..., live=True)` supports file watching.

## Basic usage flow

1. Install:

```bash
pip install -U cocoindex
```

2. Set internal state DB:

```bash
export COCOINDEX_DB=./cocoindex.db
```

or put in `.env`:

```bash
COCOINDEX_DB=./cocoindex.db
```

3. Define processing functions with `@coco.fn`.

4. Read source data, e.g. `localfs.walk_dir(...)`.

5. Mount one component per item with `coco.mount_each(...)`.

6. Declare target state such as files or DB rows.

7. Run once:

```bash
cocoindex update main.py
```

8. Run live:

```bash
cocoindex update main.py -L
```

9. Inspect:

```bash
cocoindex ls main.py
cocoindex show main.py --tree
```

10. Reset/drop target state:

```bash
cocoindex drop main.py -f
```

## Common patterns worth borrowing

### File transformation

Read files from `localfs.walk_dir`, transform each file, and declare output files with `localfs.declare_file`. Source deletion can remove corresponding output target state.

### Vector embedding pipeline

Read Markdown/PDF/text files, split with `RecursiveSplitter`, embed with `SentenceTransformerEmbedder` or another embedding function, and declare rows in Postgres/SQLite/Qdrant/LanceDB/etc.

### LLM extraction pipeline

Use `@coco.fn(memo=True)` around LLM extraction so unchanged content does not re-call the model. Declare extracted entities/statements as rows or graph nodes.

### Knowledge graph pipeline

Examples include conversation/podcast content to structured knowledge graph using extracted statements/entities and graph DB targets.

## Relevance to Yuto second brain

CocoIndex should not replace Yuto's Markdown knowledge base.

Recommended architecture:

```text
Markdown KG = source of truth
CocoIndex = derived incremental index/cache
Yuto = verifier + router
```

Potential Yuto uses:

- incrementally index `/Users/kei/kei-jarvis/knowledge/*.md`
- create a fresh metadata index: path, title, headings, wikilinks, mtime, content hash
- later add chunking and local embeddings
- later add graph-derived indexes from wikilinks/headings
- support source-backed retrieval with path/heading/line references

## Recommended pilot for Yuto

Phase 1: no embeddings.

Input:

```text
/Users/kei/kei-jarvis/knowledge/*.md
```

Target:

- local SQLite or local file output
- metadata only: note path, title, headings, wikilinks, content hash, modified time

Success criteria:

- changed note updates derived index
- deleted note removes derived target state
- index can be rebuilt from source files
- no secrets or private legal evidence ingested
- Markdown remains source of truth
- retrieved records cite original file path and heading

Phase 2:

- add chunking
- add local embedding only
- use for search, not as truth

Phase 3:

- compare graph extraction with `second_brain.py status`
- keep `broken=0`, `orphans=0`

## Relevance to AI-era legal advocacy company

CocoIndex could become a useful internal indexing layer for synthetic or consented case packets:

- evidence working-copy metadata
- timeline records
- evidence index
- lawyer-ready retrieval
- anonymized policy-pattern database

Guardrails:

- original evidence read-only
- hash/provenance before ingest
- no cloud embeddings for sensitive data without explicit consent
- target index is derived and disposable
- human review before legal output

## Risks and cautions

- PyPI marks the project Alpha; expect API and operational changes.
- It adds dependency and operational complexity.
- Pipeline bugs can produce stale or wrong derived indexes that look authoritative.
- Vector search must not become source of truth.
- Embedding/LLM providers can leak sensitive data if misconfigured.
- `cocoindex drop` can delete target state; use only in sandbox or with explicit approval.
- Stable component paths matter; unstable paths cause reprocessing or wrong cleanup.
- Use memoization carefully for expensive transforms.

## Decision

Adopt conceptually and pilot in sandbox.

Do not migrate Yuto second brain core yet.

Best first move:

> Build a minimal local CocoIndex sandbox that indexes synthetic Markdown notes or a copy of selected non-sensitive Yuto knowledge notes into a disposable SQLite/local target.

Related: [[second-brain-dashboard]], [[source-openkb]], [[source-mempalace]], [[ai-era-legal-advocacy-company-blueprint]], [[security]]
