# Reflection Pipeline Runtime Guards

Small scripts that turn the memory-safety doctrine into checkable runtime behavior.

## Scripts

- `export_session_candidate.py`
  - reads a Hermes `session_*.json`
  - writes a redacted Markdown archive to `conversation-archive/redacted/`
  - writes a quarantine candidate template to `conversation-reflections/candidate/`
  - never promotes memory automatically

- `candidate_canary.py`
  - validates candidate/promoted/rejected/stale reflection files for required provenance and promotion fields
  - rejects promoted files that still rely on `trust_level: model_inferred`

- `run_event_checkpoint.py`
  - safe event-driven wrapper for autonomous-growth checkpoints
  - chooses the latest Hermes session by default or accepts an explicit session path
  - creates redacted archive + candidate template idempotently
  - writes `lab-ops/status/reflection_checkpoint_latest.json`
  - never promotes memory automatically

## Example

```bash
cd /Users/kei/kei-jarvis
python3 tools/reflection_pipeline/run_event_checkpoint.py --trigger after-complex-task
python3 tools/reflection_pipeline/candidate_canary.py
```

Supported triggers:

- `manual`
- `after-complex-task`
- `context-compression`
- `model-switch`
- `session-close`
- `user-correction`
- `background-agent-finished`

Raw logs remain under `~/.hermes/sessions/*.json`; these scripts only create redacted/reviewable artifacts. Yuto must review a candidate before promoting anything to active memory, knowledge, or skills.
