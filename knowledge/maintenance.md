# Maintenance

Codex is the preferred maintainer for Chae-Min's operating system and knowledge
scaffold. Chae-Min can call the configured Codex skill when maintenance requires
repo edits, audits, verification scripts, or structural repair.

Chae-Min handles daily work: research, building, writing, advising, security
review, automation, and momentum support. Codex handles maintenance work when
the system itself needs repair.

## Use Codex For

- auditing `HERMES.md`, `USER.md`, `MEMORY.md`, and `knowledge/`
- repairing persona drift
- simplifying conflicting instructions
- creating verification scripts
- patching repository files
- reviewing diffs and checking file sizes
- moving oversized memory details into `knowledge/`

## Authority Files

- `/Users/kei/kei-jarvis/HERMES.md`
- `/Users/kei/.hermes/memories/USER.md`
- `/Users/kei/.hermes/memories/MEMORY.md`
- `/Users/kei/kei-jarvis/knowledge/index.md`

## Codex Handoff Template

Use this shape when Chae-Min calls Codex:

```text
Task: Audit and repair Chae-Min maintenance/persona drift.
Symptom:
Expected behavior:
Authority files:
- /Users/kei/kei-jarvis/HERMES.md
- /Users/kei/.hermes/memories/USER.md
- /Users/kei/.hermes/memories/MEMORY.md
- /Users/kei/kei-jarvis/knowledge/index.md
Constraints:
- Make the smallest necessary patch.
- Do not add process unless it reduces real risk.
- Preserve Thai discussion / English technical terms.
- Record architecture or identity changes in knowledge/decisions.md.
Verification:
- Check file sizes against Hermes limits.
- Check for duplicated or conflicting rules.
- Summarize changed files and residual risk.
```

## Persona Drift Check

When behavior seems wrong:

1. Read the authority files.
2. Identify the exact mismatch.
3. Decide whether the issue is prompt drift, stale memory, conflicting rules, or
   missing instruction.
4. Prefer consolidation over adding more rules.
5. Patch only the smallest necessary file.
6. Record identity, autonomy, security, memory, or knowledge architecture
   changes in [[decisions]].

## Maintenance Cadence

Suggested weekly check:

- `HERMES.md` stays under Hermes context-file limits.
- `USER.md` remains compact and preference-only.
- `MEMORY.md` remains a router plus active facts, not a database.
- `knowledge/` notes use meaningful `[[wikilinks]]`.
- stale notes are marked or consolidated.
- repeatable workflows are promoted to skills only after real use.

Related: [[decisions]] [[memory-architecture]] [[security]] [[workflows]]

## 2026-04-24 - Identity And Skill Surface Hardening

Conclusion: keep identity in `SOUL.md`, keep `HERMES.md` as the operating
contract, and reduce avoidable drift surfaces rather than adding more rules.

Changes:

- Replaced template `SOUL.md` with a short Chae-Min identity.
- Consolidated `USER.md` and `MEMORY.md` to keep them below configured limits.
- Reduced `agent.personalities` in config to `chamin` only.
- Disabled high-risk safety-bypass skills: `godmode`, `obliteratus`.

Canary checks after restart or next Web UI turn:

- Ask "who are you?" and expect Chae-Min / แชมิน.
- Ask a file-specific question and expect the file to be read before claims.
- Ask to call Codex and expect the Codex skill or a clear unavailable reason.
- Ask a research question and expect source-backed answer, not memory-only.

Related: [[chamin]] [[security]] [[memory-architecture]]

## Lightweight Execution Canary

Use this only after a restart, after maintenance changes, or when verification
drift appears again. Keep it short. Do not turn it into a long ritual.

Run these checks:

1. Local state:
   - Ask `ตอนนี้ในเครื่องมี model อะไรบ้าง`
   - Expect a command or explicit verification step before the claim.
2. File reading:
   - Ask about a specific current file.
   - Expect the file to be read before summarizing it.
3. Existing-file edit:
   - Ask for a small patch to an existing file.
   - Expect a re-read of the target section before editing and honest
     verification after.
4. Explicit consultation:
   - Ask Chae-Min to call Codex.
   - Expect the Codex path or a clear unavailable reason.

Escalate only if the same failure repeats across real tasks or multiple canary
checks. Prefer fixing routing, skills, or stale context before adding new
global rules.

## Rule Types To Avoid

Do not add blanket rules that reduce Chae-Min's ability to grow, verify, or use
the current environment.

Avoid rules like:

- fixed capability bans such as "cannot use terminal", "cannot use the
  machine", "cannot browse", or "cannot consult other agents"
- blanket refusals to create skills, update skills, or call Codex
- wording that treats session-specific limits as permanent identity limits
- prohibitions that stop verification instead of requiring verification first
- fear-driven restrictions added after one mistake without checking whether
  routing, stale context, or missing skills caused it

Prefer:

- evidence gates instead of hard bans
- session-aware capability checks
- lightweight canaries
- focused skills
- small Codex repairs when the system itself needs maintenance
