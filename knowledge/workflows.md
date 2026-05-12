# Workflows

Recurring Yuto-native workflows after the 2026-04-26 work reset.

## Brake Check

Use for meta/companion questions such as: enoughness, overbuild, difficulty, reset, direction, or whether to continue.

Output:
- Mode: THINK / PLAN / EXECUTE / WATCH
- Fact
- Inference
- Brake
- Smallest useful next step
- Recommendation

## One-Loop Execution

Use when Kei asks Yuto to act but the scope is open-ended.

Steps:
1. Identify one high-leverage safe action.
2. Gather prerequisites with read-only checks.
3. Execute only the selected loop.
4. Verify with proportional evidence.
5. Report before/action/after/verification/status/residual risk.
6. Stop unless Kei asks to continue.

## Research OS Loop

Use when Kei asks Yuto to read, learn, compare, or discuss a source.

Steps:
1. Read the primary source first.
2. Extract facts, patterns, and unknowns.
3. Connect the pattern to Kei/Yuto.
4. Store only reusable source trails or growth patterns.
5. Recommend one smallest next step, then stop.

## RLM-Style Research/Control Loop

Use when Kei asks for research, synthesis, self-improvement, or a large-context question.

Steps:
1. Keep root context focused on Kei's question and success criteria.
2. Identify where the true context lives: URL, paper, local note, repo, log, prior session, or corpus.
3. Peek at structure first: title/date/author/headings/source type.
4. Inspect selectively with tools: browser extraction, search, file read, terminal/code, session_search.
5. Decompose only if needed; subagents must have objective, bounded context, output schema, and stop condition.
6. Merge with source trail: fact / inference / unknown / residual risk.
7. Route durable source trails to `knowledge/`, not active memory bulk.

Reference: [[source-recursive-language-models]] [[yuto-recursive-context-operator]]

## Yuto Team Lanes

Use [[yuto-team-lanes-reuse-playbook]] when a task needs reusable worker-lane routing, least-privilege boundaries, receipts, schema validation, or safe handling of untrusted documents.

Fast rule:
- Use Yuto alone for small tasks.
- Use Team Lanes when untrusted input, high-risk claims, final artifacts, or multiple separable phases require reader/reviewer/writer separation.
- Keep worker count small: default max 3 workers, one writer, one critic.

Evaluation:
- For substantial tasks, score with [[yuto-rlm-evaluation-plan]] before claiming the new loop is effective.
- Do not claim RLM-style improved Yuto until scored task evidence exists.
- Use `python3 tools/rlm_eval.py validate <entry.json>` and append valid entries to `knowledge/yuto-rlm-task-log.jsonl`.
- Review `python3 tools/rlm_eval.py summary knowledge/yuto-rlm-task-log.jsonl` after 3 and 10 logged tasks.

## Maintenance Command Bundle

Use [[yuto-maintenance-command-center]] when running graph/tests/canary/memory/RLM-eval checks. It is the canonical command bundle for Yuto core maintenance.

## Second Brain Use / Retrieval

Use when Kei asks to find, store, or inspect reusable Yuto knowledge.

Fast commands:
1. `cd /Users/kei/kei-jarvis`
2. `python3 tools/second_brain.py status` for graph-backed entrypoint status.
3. `python3 tools/second_brain.py search "<topic>"` before relying on memory.
4. `python3 tools/second_brain.py new "<title>" --type source --why "..." --evidence "path/URL" --next "..."` for a small evidence-first capture.

Rules:
- Search `knowledge/*.md` first for reusable context.
- Use `session_search` for older conversation detail that is not durable knowledge.
- Do not store raw dumps; capture source path/URL, why it matters, evidence, and next action.

## Memory/KG Promotion

Use when a durable lesson appears.

Rules:
- Stable Kei preference -> `USER.md`
- Active pointer/high-risk reminder -> `MEMORY.md`
- Larger context -> `knowledge/`
- Repeatable procedure -> skill
- Old session detail -> `session_search`

Related: [[memory-system]] [[maintenance]] [[decisions]] [[yuto-growth-loop]]
