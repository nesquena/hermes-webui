# Source: OpenKB

Created: 2026-04-30
Source URL: https://github.com/VectifyAI/OpenKB
Repository snapshot checked: GitHub page + GitHub API/raw files on 2026-04-30

Conclusion:
OpenKB is a useful reference for Yuto's second-brain direction: compile raw documents into a persistent Markdown wiki with summaries, concept pages, cross-links, query/chat, lint, watch mode, and Obsidian compatibility. It is not something to blindly replace Yuto's current knowledge folder with yet; the best near-term move is to borrow the workflow ideas and optionally pilot OpenKB in an isolated sandbox.

Key source facts:
- README defines OpenKB as an open-source CLI that compiles raw documents into a structured, interlinked wiki-style knowledge base using LLMs, powered by PageIndex for vectorless long-document retrieval.
- Repo metadata checked from GitHub page: public repo, about 945 stars, 89 forks, 6 issues, 2 PRs, latest visible commit `798731b` "update readme" about 2 weeks before check.
- `pyproject.toml` version: `0.1.3`; classifier: Development Status :: 3 - Alpha; license: Apache-2.0; Python >=3.10.
- Dependencies include `pageindex==0.3.0.dev1`, `markitdown[all]`, `litellm`, `openai-agents`, `click`, `watchdog`, `pyyaml`, `python-dotenv`, `json-repair`, `prompt_toolkit`, and `rich`.
- OpenKB default KB layout from README: `raw/`, `wiki/index.md`, `wiki/log.md`, `wiki/AGENTS.md`, `wiki/sources/`, `wiki/summaries/`, `wiki/concepts/`, `wiki/explorations/`, `wiki/reports/`.
- Commands from README: `openkb init`, `openkb add`, `openkb query`, `openkb chat`, `openkb watch`, `openkb lint`, `openkb list`, `openkb status`.
- Config uses `.openkb/config.yaml` with model, language, and `pageindex_threshold`; API key pattern uses `LLM_API_KEY`; optional PageIndex cloud uses `PAGEINDEX_API_KEY`.
- Local environment check on 2026-04-30: `openkb` CLI was not installed and `import openkb` was false.

Yuto relevance:
- Strong match: Markdown source of truth, wikilinks, Obsidian compatibility, source/summaries/concepts separation, linting, watch mode, query/chat over compiled knowledge.
- Difference: Yuto already has active memory + skills + graph checks + session_search; OpenKB's model is document-ingestion-centric, not persona/control-plane memory by itself.
- Best borrowing target: add an ingestion pipeline and concept synthesis layer around Yuto's existing `knowledge/`, not a wholesale migration.

Risks / cautions:
- Alpha package and PageIndex dev dependency mean operational risk; do not install into Yuto's main environment without a sandbox.
- LLM compilation can introduce hallucinated cross-links or over-summarization; keep source paths and evidence mandatory.
- API-key handling uses generic `LLM_API_KEY`; map carefully to Kei's provider setup and never expose keys.
- Watch mode could mutate knowledge continuously; start manual/read-only before automation.

Recommended pilot:
1. Create an isolated sandbox KB, not inside Yuto core knowledge.
2. Install OpenKB in a disposable venv.
3. Feed 2-3 non-sensitive docs already suitable for Yuto's second brain.
4. Compare generated `wiki/concepts/` against Yuto's current `knowledge/` for retrieval quality, hallucinated links, and maintenance friction.
5. Only then decide whether to adapt OpenKB ideas into `tools/second_brain.py` or keep OpenKB separate.

Canary questions:
- Does it preserve source evidence/path for every synthesized concept?
- Does query output cite wiki/source pages, not just confident summaries?
- Does it improve retrieval over `tools/second_brain.py search` for Kei's real questions?
- Does watch mode create useful updates or noise?

Related: [[second-brain-dashboard]] [[workflows]] [[sources]] [[yuto-graph-second-brain-plan]]
