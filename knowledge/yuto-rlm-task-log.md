# Yuto RLM Task Log

Created: 2026-04-29
Purpose: compact rolling record of meaningful research/control tasks scored against [[yuto-rlm-evaluation-plan]].
Related: [[yuto-rlm-evaluation-plan]] [[workflows]] [[source-recursive-language-models]]

## How To Use

Do not log every chat turn. Log only substantial research/control tasks where RLM-style operation should matter.

1. Create a temporary JSON entry using the schema below.
2. Validate it:

```bash
cd /Users/kei/kei-jarvis
python3 tools/rlm_eval.py validate /path/to/entry.json
```

3. Append it to the machine-readable log:

```bash
python3 tools/rlm_eval.py append /path/to/entry.json knowledge/yuto-rlm-task-log.jsonl
```

4. Check summary:

```bash
python3 tools/rlm_eval.py summary knowledge/yuto-rlm-task-log.jsonl
```

## JSON Entry Schema

```json
{
  "date": "2026-04-29",
  "task": "short task name",
  "mode": "RESEARCH",
  "external_context": "source URL/file/log/session pointer",
  "rlm_style_checks": [1, 2, 3, 4],
  "source_grounding": 0,
  "context_efficiency": 0,
  "answer_usefulness": 0,
  "verification_closure": 0,
  "rework_count": 0,
  "evaluator": "yuto-self|codex-review|gemma-review|kei-review",
  "evidence_link": "source URL/file/log/session pointer used to justify the score",
  "notes": "short note only"
}
```

## Check IDs

1. External context identified before answering.
2. Source/file/log/page structure inspected before summarizing.
3. Root answer stayed focused instead of carrying all context in prose.
4. Selective operations used: search/read/compare/count/cluster/code/tool inspection.
5. Decomposition used only when useful, with bounded subtask contract.
6. Closed with fact/inference/unknown/residual risk and source trail.

## Current Summary

As of 2026-04-29, 10 real tasks have been logged.

Latest machine summary:

```json
{
  "count": 10,
  "rlm_style_count": 10,
  "rlm_style_rate": 1.0,
  "averages": {
    "source_grounding": 2.9,
    "context_efficiency": 2.7,
    "answer_usefulness": 2.8,
    "verification_closure": 3.0
  },
  "average_rework_count": 0.3,
  "evaluator_counts": {
    "codex-review": 1,
    "gemma-review": 1,
    "yuto-self": 8
  },
  "tasks_remaining_to_effective_review": 0,
  "thresholds_met": true,
  "status": "effective"
}
```

Interpretation: first 10-task review passes the configured thresholds. This supports a narrow claim: Yuto's RLM-style research/control loop is working on the evaluated maintenance/research tasks so far. It does not prove broad long-term behavioral reliability, and future tasks should continue to log evaluator/evidence_link fields.
