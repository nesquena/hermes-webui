# Yuto RLM Evaluation Plan

Created: 2026-04-29
Purpose: measure whether Yuto is actually using RLM-style operation and whether it improves outcomes.
Related: [[source-recursive-language-models]] [[yuto-recursive-context-operator]] [[workflows]] [[memory-system]] [[sources]]

## Core Question

Is Yuto actually operating RLM-style, and does it improve research/control-plane usefulness?

Do not trust vibes. Measure task traces and outcome quality.

## What Counts As RLM-Style Use

A task counts as RLM-style only if at least 4 of 6 are true:

1. Yuto identifies the external context source before answering.
2. Yuto inspects source/file/log/page structure before summarizing.
3. Yuto keeps root answer focused instead of pasting or holding all context in active prose.
4. Yuto uses selective operations: search, read sections, extract headings, compare, count, cluster, or code/tool inspection.
5. Yuto decomposes only when useful, with bounded subtask/objective/output/stop condition.
6. Yuto closes with fact/inference/unknown/residual risk and source trail.

## Primary Metrics

Track per meaningful research/control task:

- `source_grounding`: 0-3
  - 0 = no source opened
  - 1 = source opened but weak citation
  - 2 = key claims tied to source/file
  - 3 = source nuance + limitations preserved

- `context_efficiency`: 0-3
  - 0 = broad context dump / wandering
  - 1 = some selective reading
  - 2 = clear peek/search/section targeting
  - 3 = context stayed compact while preserving key evidence

- `answer_usefulness`: 0-3
  - 0 = generic answer
  - 1 = decent summary
  - 2 = actionable synthesis
  - 3 = directly changes Kei/Yuto decision or next action

- `verification_closure`: 0-3
  - 0 = claims without evidence
  - 1 = partial evidence
  - 2 = source/file/command-backed claims
  - 3 = includes residual risk and verification limit

- `rework_count`: integer
  - number of Kei corrections needed for source nuance, hallucination, overclaim, or wrong scope

## Success Threshold

After 10 meaningful research/control tasks, RLM-style is considered useful if:

- average `source_grounding` >= 2.4
- average `context_efficiency` >= 2.2
- average `answer_usefulness` >= 2.2
- average `verification_closure` >= 2.4
- `rework_count` trends down or stays <= 1 per task

If not met, inspect failures and patch workflow/skill instead of adding global rules.

## Before/After Baseline

Baseline failure patterns already observed:

- oversimplified RLM explanation into a slogan
- overclaim risk around "never gets lost"
- tendency to convert source into Yuto application too early
- prior coding/ops claim drift when verification was skipped

Expected improvements:

- fewer source-nuance corrections from Kei
- more answers cite exact source/file/command trail
- less context bloat in final answer
- clearer distinction between source fact and Yuto-specific application
- less unnecessary coding-agent behavior

## Lightweight Task Log Template

Use only for meaningful tasks, not every chat turn. Each score must identify who scored it and the evidence pointer used.

```yaml
date:
task:
mode: THINK|RESEARCH|PLAN|EXECUTE
external_context:
rlm_style_checks: [1,2,3,4]
source_grounding: 0
context_efficiency: 0
answer_usefulness: 0
verification_closure: 0
rework_count: 0
evaluator: yuto-self|codex-review|gemma-review|kei-review
evidence_link: source URL/file/log/session pointer used to justify the score
notes:
```

Store logs only as a compact rolling note if needed; do not put them in active memory.

## Fast Manual Canary

At the end of a substantial research/control task, ask:

1. Did I open/inspect the real source?
2. Did I avoid turning source into a slogan too early?
3. Did I distinguish source fact from Yuto application?
4. Did I keep only pointers/patterns in memory/KG?
5. Would Kei need fewer corrections next time?

## Review Cadence

- After 3 tasks: quick qualitative check.
- After 10 tasks: metric review.
- If 2 failures repeat: patch `yuto-knowledge-autopilot` or `workflows.md`.
- If a workflow repeats successfully several times: consider promoting to a focused skill.

## Status

Evaluation plan operationalized with `tools/rlm_eval.py`, `tests/test_rlm_eval.py`, and `knowledge/yuto-rlm-task-log.md/jsonl`. Entries require `evaluator` and `evidence_link` so task scores are traceable. The first 10-task review on 2026-04-29 passed configured thresholds with `status=effective`, but the claim is narrow: effective for the evaluated Yuto maintenance/research-control tasks so far, not proof of broad long-term reliability.
