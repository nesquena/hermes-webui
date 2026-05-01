# Yuto Maintenance Command Center

Created: 2026-04-29
Purpose: one place for the core maintenance commands Yuto should use instead of remembering long command chains.
Related: [[maintenance]] [[memory-system]] [[workflows]] [[yuto-rlm-evaluation-plan]] [[yuto-rlm-task-log]]

## Core Daily/On-Demand Check

Use this for Yuto core maintenance closure:

```bash
cd /Users/kei/kei-jarvis
python3 -m tools.yuto_graph.build_graph \
  --root /Users/kei/kei-jarvis/knowledge \
  --memory-file /Users/kei/.hermes/memories/USER.md \
  --memory-file /Users/kei/.hermes/memories/MEMORY.md \
  --extra-root /Users/kei/.hermes/skills/software-development \
  --extra-root /Users/kei/.hermes/skills/yuto \
  --extra-root /Users/kei/.hermes/skills/yuto-maintenance-audit \
  --out /Users/kei/kei-jarvis/knowledge/.graph
python3 -m pytest tests/test_yuto_graph.py tests/test_rlm_eval.py tests/test_reflection_pipeline.py tests/test_semantic_graph_audit.py -q
python3 tools/reflection_pipeline/candidate_canary.py
python3 -m tools.yuto_graph.semantic_audit \
  /Users/kei/kei-jarvis/knowledge \
  /Users/kei/.hermes/memories/USER.md \
  /Users/kei/.hermes/memories/MEMORY.md \
  --out /Users/kei/kei-jarvis/knowledge/.graph
python3 tools/rlm_eval.py summary knowledge/yuto-rlm-task-log.jsonl
```

Healthy targets:

- core graph: `broken=0`, `orphans=0`
- tests pass
- candidate canary: `ok: true`
- semantic audit: review reported duplicate titles, stale mutable claims, and weak source trails; zero is ideal, but small reviewed counts can be acceptable when evidence is intentionally historical
- RLM eval status is honestly reported: `no_data`, `collect_more_data`, `effective`, or `needs_workflow_patch`

## Memory Pressure Check

```bash
cd /Users/kei/kei-jarvis
python3 - <<'PY'
from pathlib import Path
files = [
    '/Users/kei/.hermes/memories/USER.md',
    '/Users/kei/.hermes/memories/MEMORY.md',
    '/Users/kei/kei-jarvis/HERMES.md',
    '/Users/kei/kei-jarvis/knowledge/index.md',
    '/Users/kei/kei-jarvis/knowledge/yuto.md',
    '/Users/kei/kei-jarvis/knowledge/memory-system.md',
    '/Users/kei/kei-jarvis/knowledge/workflows.md',
]
for f in files:
    p = Path(f)
    text = p.read_text(encoding='utf-8')
    print(f'{p}: chars={len(text)} lines={text.count(chr(10))+1}')
PY
```

## RLM Task Logging

Validate a scored entry:

```bash
cd /Users/kei/kei-jarvis
python3 tools/rlm_eval.py validate /path/to/entry.json
```

Append a scored entry:

```bash
python3 tools/rlm_eval.py append /path/to/entry.json knowledge/yuto-rlm-task-log.jsonl
```

Summarize scored entries:

```bash
python3 tools/rlm_eval.py summary knowledge/yuto-rlm-task-log.jsonl
```

## Rules

- Do not use the knowledge-only graph for closure if `index.md` links to active memory or core skills.
- Do not use full `/Users/kei/.hermes/skills` graph for core closure unless doing skill-vault linting; it includes plugin/reference/template noise.
- Do not claim RLM effectiveness until scored task evidence supports it.
- Do not add notes/skills from routine maintenance unless they reduce repeated real friction.
