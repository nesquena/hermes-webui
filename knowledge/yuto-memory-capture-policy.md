# Yuto Memory Capture Policy

Created: 2026-05-12 JST
Status: v0.3 implemented for quarantine, privacy filter, tool-error receipt, session-summary receipt, worker receipts, auto harness failure capture, list/status, promotion to KG draft, and doctor checks

Related: [[agentmemory-deep-dive]], [[source-agentmemory]], [[memory-system]], [[source-cocoindex]], [[yuto-ai-harm-evidence-company-team-v0.2]], [[security]]

## Conclusion

Yuto should adopt selective memory-capture primitives from agentmemory, but keep Yuto's existing stack:

```text
Markdown KG = truth
CocoIndex = derived index/cache
Yuto = verifier/router
Skills/playbooks = procedural memory
Quarantine = temporary sanitized capture
Receipts = worker evidence
```

The implemented v0.3 is useful to Yuto and the team because it creates a real path for capturing errors/session lessons, worker receipts, and reviewed KG draft promotions without writing unreviewed data into the knowledge graph.

Latest verified promotion draft: [[worker-2026-05-12-yuto-promotion-gate-v03-capture-promote-v03]]
Related lane reuse reference: [[yuto-team-lanes-reuse-pattern-2026-05-11]]

## Policy

```text
Capture candidates safely.
Quarantine before memory.
Clean before indexing.
Review before promotion.
Curate before KG/skills/active memory.
```

Forbidden by default:

- raw victim evidence;
- legal case files;
- forensic originals;
- secrets/API keys/tokens/passwords;
- production data;
- private chat exports;
- APPI-sensitive data;
- raw account/bank/platform logs.

## Implemented Files

```text
tools/memory_capture/__init__.py
tools/memory_capture/privacy_filter.py
tools/memory_capture/capture.py
tools/memory_capture/harness.py
tests/test_memory_capture.py
tests/test_memory_capture_harness.py
```

Integrated with:

```text
tools/second_brain.py capture doctor
tools/second_brain.py capture status
tools/second_brain.py capture list [--kind tool_error|session_summary|worker_receipt]
tools/second_brain.py capture promote <item_id> --reviewer yuto --rationale "..."
tools/second_brain.py path quarantine
tools/second_brain.py status  # includes capture health
```

Quarantine path:

```text
/Users/kei/kei-jarvis/.memory-quarantine/
```

## Current Capabilities

### Privacy filter

`tools/memory_capture/privacy_filter.py` redacts common sensitive patterns:

- `<private>...</private>` blocks;
- OpenAI-style `sk-...` and `sk-proj-...` keys;
- `Bearer ...` tokens;
- GitHub tokens including `github_pat_...` and `gh*_*` forms;
- AWS access keys;
- private key blocks;
- common `api_key/token/password/secret/credential/auth = value` assignments.

Every redaction sets `safe_to_store=false` / `review_required=true` on captured records.

### Tool-error capture

`capture_tool_error()` writes sanitized JSONL records under:

```text
.memory-quarantine/tool-errors/YYYY-MM-DD/<session-id>.jsonl
```

It captures:

- session id;
- project;
- agent;
- tool;
- command;
- exit code;
- sanitized stderr/stdout excerpts;
- redaction report;
- review/promotion status.

### Session-summary capture

`capture_session_summary()` writes sanitized JSON records under:

```text
.memory-quarantine/sessions/YYYY-MM-DD/<session-id>.json
```

It captures:

- decisions;
- verified outputs;
- open risks;
- changed files;
- redaction report;
- review/promotion status.

### Audit log

Every capture appends to:

```text
.memory-quarantine/audit-log.jsonl
```

Events include:

- `capture_tool_error`
- `capture_session_summary`
- `capture_worker_receipt`

### Worker-receipt capture

`capture_worker_receipt()` writes sanitized JSON records under:

```text
.memory-quarantine/worker-receipts/YYYY-MM-DD/<session-id>-<task-id>.json
```

Use this for Codex/Qwen/Claude/Chamin lane outputs before Yuto promotes anything into KG or skills. It captures:

- session id;
- project;
- agent;
- lane;
- task id;
- sanitized summary/findings;
- artifact paths;
- verification status;
- next actions;
- redaction/review status.

### Auto harness failure capture

`tools/memory_capture/harness.py` runs a local command and automatically captures a sanitized `tool_error` if it fails.

Example:

```bash
python -m tools.memory_capture.harness \
  --session-id demo-auto \
  --project kei-jarvis \
  --agent codex \
  --tool terminal \
  --cwd /Users/kei/kei-jarvis \
  -- python -m pytest tests/test_memory_capture.py -q
```

This is the default automation harness pattern: commands can fail normally, but the failure becomes a sanitized quarantine receipt for Yuto/team review.

### Promotion gate

`promote_quarantine_item()` and `python tools/second_brain.py capture promote ...` create reviewed KG draft notes under:

```text
knowledge/capture-promotions/<item-id>.md
```

Safety behavior:

- default destination is only `kg-draft`;
- `review_required=true` items are blocked unless `--force-reviewed` is passed after explicit human/expert review;
- promotion writes an audit event;
- original quarantine records are not mutated or deleted;
- promotion drafts remain reviewed notes, not legal/forensic proof.

Example:

```bash
python tools/second_brain.py capture promote <item_id> \
  --reviewer yuto \
  --rationale "Verified pass receipt and useful stable workflow lesson"
```

### Doctor checks

Run:

```bash
python -m tools.memory_capture.capture doctor
python tools/second_brain.py capture doctor
```

Checks:

- quarantine directories exist;
- JSON/JSONL parse correctly;
- counts by captured kind;
- invalid files list.

## Current Verification

Tests:

```bash
python -m pytest tests/test_memory_capture.py tests/test_memory_capture_harness.py -q
python -m pytest tests/test_second_brain.py tests/test_yuto_team_lanes.py tests/test_memory_capture.py tests/test_memory_capture_harness.py -q
python tools/second_brain.py capture promote <item_id> --reviewer yuto --rationale "..."
```

Expected latest result:

```text
all tests pass; current verification should be checked with the commands above
```

## How Teams Should Use It

### Yuto

- capture candidate lessons from tool failures;
- capture session summaries;
- review quarantine before promoting to KG/skills;
- keep active memory compact.

### Codex / coding workers

- return receipts to Yuto;
- Yuto may store sanitized errors/session summaries in quarantine;
- no worker writes shared KG directly.

### Local Qwen / reviewers

- read curated KG/playbooks;
- output review receipts;
- Yuto may quarantine useful reviewer notes before promotion.

### Future Claude/other agents

- if Kei allows, they can submit receipts/candidates;
- their raw memory stays separate;
- Yuto only promotes reviewed, sanitized, useful items.

## Promotion Rules

Captured items are not truth.

Statuses:

```text
quarantined
sanitized
review_required
rejected_sensitive
promoted_to_kg
promoted_to_skill
expired_deleted
```

Promotion destinations:

| Candidate type | Destination |
|---|---|
| source-backed research | `knowledge/source-*.md` |
| operating decision | `knowledge/decisions.md` |
| repeated procedure | Hermes skill / playbook |
| active routing pointer | compact MEMORY.md entry |
| temporary/noisy item | expire/delete |

## Next Sprint

Sprint Y2 completed:

```text
python tools/second_brain.py capture list
worker receipt capture
local command auto harness failure capture
```

Sprint Y3 completed:

```text
tools/memory_capture/promote_candidate.py behavior is implemented in tools/memory_capture/capture.py
python tools/second_brain.py capture promote <item_id>
KG draft promotion under knowledge/capture-promotions/
review_required promotion block unless --force-reviewed
```

Next Sprint Y4 should add retention/expiry and retrieval improvement:

```text
retention/expiry helper
source-note and skill-candidate templates
SQLite FTS5/BM25 over CocoIndex derived JSON
benchmark 20 Yuto recall questions
compare with current lexical search
```

## Design Decision

Do not install agentmemory into Yuto core yet.

Yuto's useful path is:

```text
selective primitives from agentmemory
+ Yuto quarantine/promotion discipline
+ existing Markdown KG/CocoIndex/skills
```

This gives Yuto and the team practical benefit without creating a shared raw-memory dump.
