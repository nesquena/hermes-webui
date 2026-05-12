# Yuto Team Feature Audit 2026-05-12

Created: 2026-05-12 JST
Status: v0.1 audit doc
Purpose: inventory Yuto/team features by implementation state, observed usage, usefulness, and improvement action.

Related: [[yuto-memory-capture-policy]], [[yuto-team-lanes-reuse-playbook]], [[yuto-ai-harm-evidence-company-team-v0.2]], [[chamin-employee-team-prototype-playbook]], [[source-cocoindex]], [[agentmemory-deep-dive]], `yuto-team-lane-receipts.jsonl`, [[second-brain-dashboard]]

## 1. Bottom Line

The team system is useful, but uneven.

Keep and deepen:

- Markdown KG + graph + second_brain status;
- CocoIndex derived index/doctor;
- Yuto Memory Capture v0.3;
- Team Lanes manifests/validator/receipt measurement;
- active cron reports that already deliver useful signal.

Use more before expanding:

- Chamin team prototype;
- Workspace Swarm roster;
- company operating model workflows;
- Qwen/local reviewer lane.

Do not expand yet:

- persistent autonomous swarm runtime;
- automatic raw log/session capture daemon;
- large security/DFIR model downloads;
- promotion to active memory without review;
- more roles/divisions before live work proves value.

## 2. Evidence Snapshot

Verified live on 2026-05-12:

```text
second_brain status:
- knowledge notes: 69
- graph: nodes=112 edges=397 broken=0 orphans=0
- CocoIndex: source_notes=69 derived_json_files=69 ok=true
- capture: ok=true invalid=[]
- capture counts: tool_error=2, session_summary=1, worker_receipt=2, audit=6

focused tests:
- tests/test_memory_capture.py
- tests/test_memory_capture_harness.py
- tests/test_second_brain.py
- tests/test_yuto_team_lanes.py
- tests/test_chamin_team.py
- tests/test_chamin_team_cli.py
- result: 36 passed

Team lanes:
- lane manifest validation: ok=true
- lane ids: 7 lane manifests
- receipts: 10 total, 10 pass, 0 partial, 0 fail
- average_rework: 0.4
- caveat: receipts are mostly retrospective/directional, not enough prospective proof

Chamin prototype:
- status: OK
- lanes: 6
- cookbooks: 1
- validator: ok=true, files_checked=11
- tests: 10 passed

Cron jobs:
- Yuto daily lightweight maintenance audit: enabled, last_status=ok, deliver=local
- AI News Radar to Telegram: enabled, last_status=ok, deliver=telegram
- Yuto Team Lanes 7-Day Evaluation Report to Telegram: enabled, scheduled, not run yet at audit time

Local model state:
- ollama list includes qwen3.6:27b and bge-m3:latest
- ollama ps shows qwen3.6:27b loaded, 100% GPU, context 32768, keepalive Forever
- bge-m3 loaded, 100% GPU, context 8192, keepalive Forever

Hermes Workspace:
- package.json version: 2.3.0
- swarm.yaml exists with Yuto lane-based roster
```

## 3. Rating Legend

Implementation status:

- `done`: usable and verified by file/command/test.
- `partial`: exists, but missing runtime, live usage, or safety/completion loop.
- `planned`: documented but not implemented.
- `park`: keep as reference; do not invest now.

Observed usage:

- `frequent`: repeatedly used in current Yuto workflow or verification loop.
- `sometimes`: used during setup/audit or useful for specific tasks.
- `rare`: exists but little evidence of real use.
- `none-yet`: implemented/planned but no live usage evidence.

Usefulness:

- `high`: directly improves Yuto/team correctness, speed, safety, or recall.
- `medium`: useful in specific contexts, but not daily core.
- `low`: mostly reference/overhead unless revived by a concrete task.
- `negative-risk`: could add noise/risk if expanded too soon.

## 4. Feature Inventory

| Feature | Status | Observed usage | Usefulness to Yuto | Usefulness to team | Keep / change |
|---|---|---:|---:|---:|---|
| Markdown KG in `knowledge/` | done | frequent | high | high | Keep as source of truth. Keep notes curated; avoid raw dumps. |
| Graph build/status | done | frequent | high | medium | Keep; use as hygiene gate after doc changes. |
| `tools/second_brain.py status/search/new/path` | done | frequent | high | medium | Keep; make this the main local control CLI. |
| CocoIndex derived JSON index | done | frequent during build; sometimes operational | high | medium | Keep; add FTS/BM25 later only after recall benchmark. |
| CocoIndex doctor | done | frequent | high | medium | Keep; run after KG changes. |
| Obsidian bridge | done | sometimes | medium | low | Keep; useful for human browsing, not core automation. |
| Memory Capture privacy filter | done | frequent in capture flow | high | high | Keep; expand patterns only from real misses. |
| Quarantine root `.memory-quarantine` | done | frequent in capture flow | high | high | Keep outside KG; never index raw quarantine. |
| Tool-error capture | done | sometimes; 2 records | high | high | Keep; use through harness for failed commands. |
| Session-summary capture | done | rare; 1 record | medium | medium | Use more at end of larger work sessions. |
| Worker receipt capture | done | sometimes; 2 records | high | high | Make required for real worker/lane tasks. |
| Auto failure harness | done | sometimes; smoke-tested | high | high | Use for tests/builds/scripts that might fail. Do not make daemon yet. |
| Capture list | done | sometimes | high | high | Keep; this is the review queue. |
| Capture promote to KG draft | done | sometimes; 1 promotion | high | high | Keep; default route for useful reviewed lessons. |
| `review_required` promotion block | done | tested | high | high | Keep; this is key safety control. |
| Team Lanes playbook | done | sometimes | high | high | Keep; use only when task complexity justifies lanes. |
| Team lane manifests | done | sometimes | high | high | Keep; add manifests only after live need. |
| Team lane validator | done | frequent in verification | high | high | Keep; run before claiming roster health. |
| Team lane receipt log | done | sometimes; 10 receipts | medium | high | Keep, but require 3-5 prospective receipts next. |
| Hermes Workspace Swarm roster | done | rare/sometimes | medium | medium | Keep as visual roster/control-room, not proof of live workers. |
| Persistent runtime swarm | planned | none-yet | low now | medium later | Do not build until prospective lane tasks prove value. |
| AI Harm Evidence Company org doc | done/design | sometimes | high strategic | high strategic | Keep as strategy; operationalize only 3 workflows first. |
| 10-division / 65-role company model | done/design | rare operationally | medium | high future | Do not add more roles; select 16 priority roles only. |
| Daily Global AI Harm Scan workflow | partial/planned | AI News Radar cron exists | high | high | Keep; align cron output to company workflow. |
| Weekly Intelligence Brief | planned | none-yet | medium | high | Add only after daily radar signal is stable. |
| Monthly Research & Policy Memo | planned | none-yet | medium | high | Later; needs source discipline and scope. |
| Synthetic Case Evaluation workflow | planned | none-yet | high future | high future | Important, but do after evidence SOP is written. |
| Product Prototype Loop | planned | none-yet | high future | high future | Later; requires actual prototype scope. |
| Chamin team prototype | done/validated | rare; no live receipts found | medium | medium-high | Run one real LawLabo PR review pilot before expanding. |
| Chamin lane/cookbook validator | done | sometimes | medium | medium | Keep; useful for Chamin only. |
| Chamin runtime wrappers | partial | none-yet | low now | medium later | Do not expand until pilot receipt exists. |
| RLM eval/log tooling | done/older | rare | low-medium | low | Park unless doing explicit RLM/control-plane evaluation. |
| Qwen auxiliary benchmark | partial | rare; latest command returned overall_pass=false | medium | medium | Fix benchmark before trusting Qwen for standing reviewer scoring. |
| Qwen3.6 local model worker | available | sometimes | medium-high | medium-high | Use as read-only reviewer/extractor; do not make main Yuto brain. |
| bge-m3 embeddings model | available | rare/infra | medium future | medium future | Useful if/when FTS/vector retrieval is implemented. |
| Specialized cyber/DFIR models | planned/park | none | low now | medium future | Benchmark first; no downloads without approval. |
| AI News Radar cron | done | frequent; last_status ok | high | high | Keep; prune if Telegram signal becomes noisy. |
| Daily maintenance audit cron | done | frequent; last_status ok | high | medium | Keep; local delivery is okay. |
| Team Lanes 7-day report cron | scheduled | none-yet | medium | high if delivered | Let it run; evaluate after first report. |
| GoalBuddy pattern | park/reference | used conceptually | medium | medium | Keep as pattern only; no dependency. |
| agentmemory | park/reference | studied deeply | high as design reference | medium | Do not adopt core; borrow selective primitives only. |
| Anthropic financial-services repo | park/reference | used conceptually | high | high | Keep as governance pattern; no dependency. |
| Source docs for KCL/Cambridge/Japan/forensic path | done | sometimes | high strategic | high strategic | Keep; update only when research moves. |
| Web performance checklist | done | sometimes | medium | medium | Keep for delivered web work reviews. |
| Systems-thinking templates | done | sometimes | medium | medium | Use before big planning/PhD/product decisions. |

## 5. Use Buckets

### 5.1 Use often / core

These should stay in Yuto's standard operating loop:

- `tools/second_brain.py status`
- graph health and broken/orphan check
- CocoIndex update/doctor after KG changes
- Team lane validator when roster/manifests change
- focused pytest for touched modules
- verify-before-claim + safe-file-edit
- Memory Capture doctor/list/promote for meaningful lessons
- AI News Radar / daily maintenance cron monitoring

### 5.2 Use sometimes / situational

Use when the task justifies the overhead:

- Team Lanes multi-worker pattern;
- Workspace Swarm roster view;
- worker receipts;
- auto harness for tests/builds/scripts;
- Chamin team prototype for PR/review workflows;
- Qwen read-only reviewer;
- company org model for strategic planning;
- systems-thinking templates.

### 5.3 Rare / not yet proven

Do not expand until there is a concrete pilot:

- Chamin runtime dispatch beyond current CLI;
- persistent multi-agent swarm;
- synthetic case evaluation workflow;
- monthly policy memo pipeline;
- RLM evaluation loop;
- Qwen auxiliary benchmark;
- bge-m3/vector retrieval integration.

### 5.4 Park / reference only

Keep as source-backed inspiration, not operating dependencies:

- agentmemory core runtime;
- GoalBuddy package;
- Anthropic financial-services repo;
- specialized cyber/DFIR community models;
- large-scale MCP/tool sprawl.

## 6. What Helps Yuto Most

1. Memory Capture v0.3: prevents losing lessons while avoiding raw memory pollution.
2. second_brain status + graph + CocoIndex doctor: gives live, checkable system health.
3. Team lane validator + receipts: prevents fake team claims.
4. Cron radar/maintenance: gives recurring external/current signal without manual prompting.
5. Company org model: keeps strategy coherent across legal, forensic, security, product, and research.

## 7. What Helps the Team Most

1. Lane manifests: make workers scoped, bounded, and reusable.
2. Worker receipts: let Yuto verify before trusting output.
3. Quarantine/promotion: gives a safe route from worker output to shared knowledge.
4. One-writer / reader-reviewer-writer separation: reduces prompt injection and evidence contamination risk.
5. Workspace roster: helps visualize the team, but should not be mistaken for runtime.

## 8. Low-Value or Risky If Expanded Now

| Item | Why not expand now | Action |
|---|---|---|
| Persistent swarm runtime | More moving parts; not enough prospective evidence | Wait for 3-5 real lane-assisted tasks. |
| More divisions/roles | Already 10 divisions/65 roles; more will become bureaucracy | Freeze org model. Prioritize 16 roles. |
| Automatic raw capture daemon | Could capture secrets/legal/evidence data | Keep explicit harness only. |
| agentmemory install | Heavy, broad, privacy risk for legal/forensic work | Keep as reference/sandbox only. |
| Specialized cyber models | Could be offensive/noisy/large; no benchmark yet | Benchmark current Qwen first; require approval for downloads. |
| Monthly reports | Risk of ritual output before useful daily/weekly signal | Build only after daily/weekly signal quality is proven. |

## 9. Improvement Backlog

### P0 — Do now / next

1. Run 3 live prospective lane-assisted tasks and append receipts.
   - target: one research scout, one code/review task, one evidence/SOP task.
   - success: receipt says saved_time or quality_gain >= medium, verification_status pass/partial with concrete evidence.

2. Make worker receipt capture the default for any delegated worker.
   - use `python -m tools.memory_capture.capture worker-receipt ...`.
   - promote only the useful reviewed ones.

3. Create a compact `capture review` command or checklist.
   - purpose: list items needing review, grouped by risk.
   - avoid building a UI first.

### P1 — Useful soon

4. Retention/expiry helper.
   - expire noisy quarantine items after review.
   - never delete without explicit command/manifest.

5. Qwen reviewer benchmark repair.
   - current `tools/qwen_aux_benchmark.py` returned `overall_pass=false` when invoked.
   - define small reviewer tasks before using Qwen as standing critic.

6. Chamin pilot.
   - run one LawLabo PR review via Chamin packet/receipt.
   - decide keep/modify/drop after evidence.

7. Align AI News Radar with company workflow.
   - map output to Global Intelligence Division.
   - add source quality labels and Japan relevance.

### P2 — Later

8. FTS5/BM25 retrieval over CocoIndex JSON.
   - only after 20 recall benchmark questions are defined.

9. Synthetic case evaluation workflow.
   - use synthetic data only.
   - include forensic/legal human review flags.

10. Workspace Swarm runtime pilot.
   - one read-only worker first.
   - verify tmux/profile/dispatch/reports before calling it operational.

## 10. Recommended Operating Rule

Use this decision rule before adding or using any feature:

```text
If it improves verification, safe memory, source recall, or worker handoff quality, keep/use it.
If it only makes the team look bigger, park it.
If it touches raw evidence, secrets, legal conclusions, publishing, or production, require human gate.
```

## 11. Next Audit Criteria

Run this audit again after 3-5 prospective lane tasks.

Minimum evidence to upgrade the team maturity score:

- at least 3 new live worker receipts;
- at least 1 promoted capture note that was reused later;
- at least 1 Chamin or Qwen reviewer pilot with pass/partial outcome;
- no broken graph links or stale CocoIndex entries;
- no raw sensitive data in quarantine promotion drafts.

## 12. Final Assessment

Current maturity:

```text
Yuto core operating system: usable and useful
Team Lanes: validated and directionally useful, needs prospective use
Memory Capture: operational v0.3
Workspace Swarm: visual roster ready, not runtime proof
Chamin: validated prototype, not live-proven
Company model: strategically strong, operationally partial
```

Best next move:

```text
Stop adding architecture for now.
Run 3 real team tasks through the harness/receipt/promotion loop.
Then prune or promote features based on evidence.
```
