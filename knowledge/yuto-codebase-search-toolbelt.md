# Yuto Codebase Search Toolbelt

Created: 2026-05-12 JST
Status: active v0.1

Purpose: give Yuto foreground a local, source-grounded codebase search tool for improving Yuto's own system without delegating Yuto-project changes to other agents.

Related: [[memory-palace]] [[second-brain-dashboard]] [[source-cocoindex]] [[yuto-memory-scout]]

## Contract

Yuto owns Yuto core/system-improvement work in the foreground.

This toolbelt is read-only retrieval support:

- use CocoIndex Code for semantic code search;
- use local lexical scoring as fallback/boost;
- search likely Yuto authority/code/test files before patching;
- do not let another agent own Yuto-project changes unless Kei explicitly asks;
- verify from files/tests/commands before claiming.

## Installed Tool

CocoIndex Code installed via:

```bash
uv tool install --upgrade 'cocoindex-code[full]'
```

Verified local embedding config:

```text
provider: sentence-transformers
model: Snowflake/snowflake-arctic-embed-xs
```

Project settings:

```text
/Users/kei/kei-jarvis/.cocoindex_code/settings.yml
```

Index database:

```text
/Users/kei/kei-jarvis/.cocoindex_code/target_sqlite.db
```

## Yuto Wrapper

```bash
cd /Users/kei/kei-jarvis
python tools/yuto_code_search.py "memory palace doctor" --limit 8
python tools/yuto_code_search.py "memory scout root detection" --json
```

The wrapper combines:

1. `ccc search` semantic results;
2. local lexical scan over source/knowledge/test files;
3. Yuto-specific boosts for memory/scout/palace/Second Brain/team receipt authority paths.

## Benchmark Gate

```bash
cd /Users/kei/kei-jarvis
python tools/yuto_code_search_benchmark.py --min-score 9.0
```

Initial acceptance result:

```text
score_10: 9.63
pass: true
```

Canaries cover:

- Memory Scout root/config/status;
- Memory Palace doctor;
- Book Expert Factory blueprint gate;
- latest raw-session recall;
- CocoIndex Second Brain health;
- team lane receipts.

## Use Before Yuto Self-Patches

Before editing Yuto core files, search first:

```bash
python tools/yuto_code_search.py "<topic>" --limit 8
```

Then read the selected files directly, patch minimally, and run targeted tests.

## Boundaries

- This is not authority; files/tests/commands remain authority.
- This is not a replacement for LSP diagnostics.
- This should not load the whole repo into prompt.
- This should not index secrets or private raw sessions.
- Benchmark score is a routing canary, not proof of code correctness.
