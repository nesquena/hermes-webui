# Source: MemPalace

Checked: 2026-05-04
Source URL: https://github.com/MemPalace/mempalace
Official docs per README: https://mempalaceofficial.com
Official PyPI per README: https://pypi.org/project/mempalace/

## What it is

MemPalace is a local-first AI memory system. Its README describes the core design as verbatim conversation/project storage plus semantic retrieval, not summarization/extraction. It structures memory as wings, rooms, drawers, and supports scoped search.

## Source facts verified

- GitHub repo: `MemPalace/mempalace`, public, MIT license, default branch `develop`.
- GitHub API on 2026-05-04 showed: created 2026-04-05, pushed 2026-05-03, ~50,934 stars, ~6,690 forks, 511 open issues, language Python, topics include `ai`, `chromadb`, `llm`, `mcp`, `memory`, `python`.
- `README.md` headline: local-first memory, verbatim storage, pluggable backend, ChromaDB default, zero API calls for raw benchmark path, explicit scam-domain warning.
- `pyproject.toml`: package `mempalace` version `3.3.4`, Python `>=3.9`, status Beta, dependencies `chromadb>=1.5.4,<2`, `pyyaml`, optional hardware acceleration extras, CLI entrypoints `mempalace` and `mempalace-mcp`.
- Source modules inspected: `mempalace/cli.py`, `mempalace/mcp_server.py`, `mempalace/backends/base.py`, `mempalace/backends/chroma.py`, `mempalace/miner.py`, `mempalace/convo_miner.py`, `mempalace/knowledge_graph.py` listed in tree.
- Tests and CI exist: `tests/` contains CLI/backend/plugin tests; `.github/workflows/ci.yml` exists.
- Benchmark files exist under `benchmarks/` with committed result JSON/JSONL files and `benchmarks/BENCHMARKS.md`.
- Local availability check on 2026-05-04: `mempalace_importable=False`, `mempalace_cli=None`, `mempalace_mcp_cli=None`.

## Yuto relevance

MemPalace is directly relevant to Yuto's memory/second-brain design because it validates a simple baseline Kei already favors: store source text with provenance and retrieve it, rather than over-summarizing active memory. Its useful ideas for Yuto are:

- Treat verbatim source/provenance as the substrate; summaries are views, not the only memory.
- Use scoped retrieval by project/person/topic, similar to Yuto's routing between active memory, knowledge notes, sources, and skills.
- Keep memory local-first and avoid cloud/API dependency for core recall.
- Consider MCP/tool interface patterns, but only after sandbox review.
- Use benchmark discipline: distinguish retrieval recall from end-to-end QA accuracy.

## Risks / cautions

- Do not install into Yuto core env yet; current local check shows it is not installed.
- README and `docs/HISTORY.md` show prior benchmark-claim corrections and scam-domain warnings. Treat metrics carefully and quote only with the exact metric, dataset, and mode.
- Default ChromaDB dependency can add storage/runtime complexity; sandbox before integrating.
- Verbatim memory can preserve sensitive content. Any pilot must define redaction, scope, retention, and deletion rules.
- High star/fork counts are signals, not proof of quality or fit.

## Recommended pilot

Pilot only in an isolated directory, not as a replacement for Yuto's current Markdown KG:

1. Create a disposable palace outside `/Users/kei/kei-jarvis`.
2. Mine a small synthetic/non-sensitive conversation corpus and a small docs folder.
3. Test scoped search, `wake-up`, MCP read-only tools, and deletion/export behavior.
4. Compare recall against current Yuto session_search + knowledge notes for 10 canary questions.
5. Decide whether to borrow architecture ideas, keep as optional external index, or ignore.

## Canary questions for a pilot

- Can it retrieve the original source wording for a known decision without summarization drift?
- Can search be scoped cleanly by project/person/topic?
- Can sensitive drawers be deleted and verified gone?
- Does MCP write access introduce accidental memory pollution?
- Does it outperform Yuto's current `session_search` + Markdown source notes on concrete recall tasks?

Related: [[sources]] [[memory-system]] [[second-brain-dashboard]] [[source-openkb]]
