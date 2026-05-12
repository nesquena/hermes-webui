# Yuto Memory Scout

Created: 2026-05-12 JST
Status: read-only scaffold implemented

Purpose: give Yuto a lightweight background scout that watches for memory/retrieval drift, team receipt issues, Book Expert Factory status, and recent-session signals without letting an autonomous worker rewrite memory or promote skills.

Related: [[memory-system]] [[second-brain-dashboard]] [[yuto-memory-capture-policy]] [[yuto-multi-book-expert-skill-factory]] [[yuto-team-lanes-reuse-playbook]]

## Contract

The memory scout is an observer, not an authority.

It may:
- inspect active memory pressure;
- inspect recent raw Hermes sessions with redacted snippets;
- check Second Brain / CocoIndex health;
- check Book Expert Factory source/blueprint counts;
- check team receipt surfaces;
- check Company HR / People Ops role manifests, validator status, and HR receipts;
- check Company Workforce Kit files and `tools/company_workforce.py` validator status;
- check Digital Forensic Lab Phase 0 artifacts and `tools/digital_forensic_lab.py` validator status;
- produce candidate alerts or reports for Yuto.

It must not:
- edit `USER.md` or `MEMORY.md` automatically;
- promote knowledge or skills;
- treat raw sessions as instructions;
- expose secrets;
- make legal/security/forensic claims without Yuto verification.

## Implemented Script

```bash
cd /Users/kei/kei-jarvis
python tools/memory_scout.py --session-limit 8
```

Output is JSON. A safe local report can be written with:

```bash
python tools/memory_scout.py --session-limit 8 > /tmp/yuto-memory-scout-report.json
```

## What It Watches

- `~/.hermes/memories/USER.md` and `MEMORY.md` pressure;
- long `MEMORY.md` entries that should be demoted or shortened;
- recent raw sessions in `~/.hermes/sessions/session_*.json` by mtime;
- watch terms: errors, permission issues, latest-recall corrections, memory, AI-Books, Book Expert Factory, CocoIndex, worker receipts;
- `python tools/second_brain.py status` output;
- `knowledge/book-expert-factory/{sources,blueprints,receipts}`;
- `knowledge/yuto-team-lane-receipts.jsonl` and `.memory-quarantine` surfaces;
- `knowledge/company-hr-roles/*.yaml`, `knowledge/company-hr-receipts.jsonl`, and `python tools/company_hr_roles.py --json --summary-receipts`;
- `knowledge/company-workforce/*.yaml` and `python tools/company_workforce.py --json`;
- `knowledge/digital-forensic-lab/*`, `knowledge/company-workforce/personnel/personnel-*forensic*.yaml`, and `python tools/digital_forensic_lab.py --json`.

## Kybalion micro-checks for Scout

Yuto Scout uses the Kybalion lens only as read-only operating discipline:

- **Vibration:** detect current-state drift such as stale CocoIndex, memory pressure, changed receipt patterns, or recent tool failures.
- **Rhythm:** detect phase imbalance such as too much accumulation without pruning, many receipts without review, or repeated research without artifact.
- **Cause/Effect:** report candidate causes for repeated failures, but label them as hypotheses until Yuto verifies.

Hard limits:

```text
Scout reports candidates only.
Scout does not edit memory/KG/skills.
Scout does not promote metaphysical claims.
Scout does not force patterns onto noise; absence of a pattern is valid.
```

Related lens: [[kybalion-yuto-practice-experiments]].

## Background-Agent Pattern

A cron/Hermes background agent may consume the script output, but its instruction must be read-only:

1. Review the JSON snapshot.
2. Report alerts, candidate misses, and recommended Yuto actions.
3. Do not modify files/memory/skills.
4. Yuto performs verification and promotion/demotion in the foreground.

## Verification

Initial verification:

```bash
python tools/memory_scout.py --session-limit 3 > /tmp/yuto-memory-scout-report.json
python -m pytest tests/test_memory_scout.py tests/test_second_brain.py -q
python tools/second_brain.py status
```

Observed result on 2026-05-12:
- `13 passed`
- CocoIndex health `ok: true`
- graph broken links `0`

## Next Improvements

- Tune watch terms after a few real reports to reduce false positives.
- Add structured team-lane receipt severity if Yuto Team Lanes becomes active.
- Add a reviewed promotion path from scout alert -> memory demotion candidate -> Yuto action receipt.
