# Capy Output Compaction Design

> **For Hermes:** This is a clean-room Phase 2 design and MVP implementation note for TokenJuice-style output compaction. Do not copy OpenHuman or TokenJuice source, schemas, tests, comments, or prompts. Use this document to extend `api/capy_compaction.py` with strict TDD.

**Goal:** Bound noisy tool, browser, test, Spaces, and subagent output before it enters model context or product receipts, while preserving action-critical error/approval evidence and redacting unsafe prompt/source/auth markers.

**Current MVP:** `api/capy_compaction.py::compact_output(...)` returns a metadata-only receipt with original/compacted character counts, rules applied, redaction status, exit status, and bounded text.

---

## Product contract

Capy output compaction should make long-running autonomous work readable without hiding important state:

- Preserve `exit_status`, nonzero failure context, first/last error lines, tracebacks, and approval prompts.
- Redact raw unsafe strings before any truncation or receipt generation.
- Collapse local repo paths to stable short paths where possible.
- Dedupe repeated noisy lines.
- Keep receipts bounded with `original_chars`, `compacted_chars`, `rules_applied`, and `redaction_status`.
- Treat compaction output as context, not authority; it must not bypass approval, creator-loop, visual-QA, sandbox, or recovery gates.

## Non-goals

- Do not delete raw artifacts that are intentionally stored in a trusted backend-only debug/quarantine location.
- Do not use LLM summarization for the MVP; the first layer is deterministic and rule-based.
- Do not claim security coverage from compaction alone. Prompt-injection preflight remains a separate Phase 5 boundary.
- Do not expose redacted line contents in audit fields, logs, or UI receipts.

## Rule model

A future configurable rule can use this shape:

```yaml
name: pytest-default
match:
  tool: terminal
  command_regex: "pytest|python -m pytest"
actions:
  - redact_unsafe_markers
  - collapse_paths
  - dedupe_repeated_lines
  - preserve_error_blocks
  - preserve_approval_prompts
  - cap_section_chars
safety:
  never_hide_nonzero_exit_status: true
  never_hide_first_last_error_block: true
  never_hide_approval_prompts: true
```

The MVP hardcodes the small safe subset until rule loading has its own tests.

## Built-in rule families

### `redact_unsafe_markers`

Replace the entire unsafe line with `[REDACTED]` before dedupe/truncation when it contains:

- Generated/executable markers: `renderer`, `<script>`, `html:`, `source:`, `source code`, `data:`, `data payload`, `body:`, `renderCode`, `generated code`, `generated body`, `widgetBody`.
- Auth/credential markers: `api_key`, `api_auth`, `authorization`, bearer/token placeholders, passwords, credentials, secret-looking sentinels.
- Prompt boundary markers: `raw prompt`, `system prompt`, `developer prompt`, `prompt injection`, `ignore previous instructions`.

### `collapse_paths`

Convert repository-local absolute paths like:

```text
/Users/bschmidy10/hermes-webui/static/spaces.js
```

to:

```text
.../static/spaces.js
```

This keeps evidence useful while reducing local path noise.

### `dedupe_repeated_lines`

Consecutive identical lines are collapsed to a single line with `(repeated Nx)`.

### `preserve_error_blocks`

If `exit_status` is nonzero or the output contains failure/error lines, preserve the exit status and first/last error lines ahead of noise trimming.

### `preserve_approval_prompts`

Approval prompts are kept ahead of noise trimming. Compaction must never silently remove a prompt that implies user approval is needed; if multiple approval lines are present, each one becomes required evidence before generic context is considered.

### `cap_section_chars`

When safe output still exceeds the requested character budget, the receipt records `cap_section_chars` and keeps required exit/error/approval evidence before optional context.

## Initial APIs

```python
from api.capy_compaction import compact_output

receipt = compact_output(
    raw_output,
    tool="terminal",
    command="pytest tests/test_capy_memory_tree.py",
    exit_status=1,
    max_chars=4000,
)
```

Receipt shape:

```json
{
  "tool": "terminal",
  "command": "pytest tests/test_capy_memory_tree.py",
  "exit_status": 1,
  "original_chars": 18000,
  "compacted_chars": 2200,
  "compacted": true,
  "rules_applied": ["collapse_paths", "preserve_error_blocks"],
  "redaction_status": "none",
  "redacted_count": 0,
  "text": "...bounded output..."
}
```

## Safety invariants

- `output` must be raw text. Structured dictionaries are rejected instead of stringified, because stringifying objects can leak hidden fields.
- Unsafe lines are redacted before truncation so tail/head slicing cannot leak them.
- Receipt metadata never stores redacted raw values.
- `max_chars` has a lower bound so callers cannot accidentally erase required exit/error/approval context.
- Approval prompts and error evidence are included before generic context when bounding.

## TDD acceptance bundle

Current tests: `tests/test_capy_output_compaction.py`

Required behavior covered:

- Preserve nonzero exit status plus first/last pytest failure lines.
- Dedupe repeated lines and collapse local repo paths.
- Redact synthetic unsafe markers without leaking them through receipt JSON.
- Reject non-string raw outputs and invalid budgets.
- Preserve approval prompts when a long output is character-bounded.

Validation commands:

```bash
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m pytest tests/test_capy_output_compaction.py -q -o 'addopts='
/Users/bschmidy10/.hermes/hermes-agent/venv/bin/python -m py_compile api/capy_compaction.py tests/test_capy_output_compaction.py
git diff --check -- api/capy_compaction.py tests/test_capy_output_compaction.py docs/capy-output-compaction.md
```

## Future slices

1. Add route/tool receipt integration for Spaces long outputs and demo smoke suites.
2. Add a product-visible `Compaction evidence` card in Spaces progress/detail surfaces.
3. Add user/project rule loading from a local, non-secret config path.
4. Add provenance/citation handles so compacted receipts point to trusted backend-only raw artifacts when raw retention is explicitly enabled.
5. Connect compaction stats to the structured progress-event stream.
