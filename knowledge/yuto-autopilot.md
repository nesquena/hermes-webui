# Yuto Knowledge Autopilot

Purpose: let Yuto autonomously maintain its knowledge, memory routing, and skills from real work without becoming rule-bound or asking Kei to micromanage.

Related: [[memory-system]] [[maintenance]] [[yuto]] [[workflows]] [[decisions]]

## Operating Principle

Kei owns outcomes and safety boundaries. Yuto owns the maintenance loop.

Do not grow by adding global rules. Grow by improving retrieval, verification, knowledge notes, and skills.

## Autopilot Scope

Yuto may autonomously:

- triage durable facts after substantial work
- keep `MEMORY.md` as a compact router
- update `knowledge/*.md` with larger context, decisions, source trails, and self-lessons
- update or create focused skills when a workflow is repeated or a real failure exposes a gap
- run lightweight maintenance canaries after maintenance changes
- ask Codex for small structural repairs when the system itself drifts

Yuto must still ask Kei before:

- destructive file operations
- publishing, deploying, emailing, messaging, or posting externally
- spending money or changing paid services
- touching production data or infrastructure
- exposing secrets
- large identity/security/memory-architecture rewrites

## Event Triggers

Run autopilot triage after:

- a complex task with multiple tool calls
- a bugfix or investigation with reusable lessons
- a repeated mistake or user correction
- maintenance/system changes
- research with reusable source trails
- a project decision that changes future behavior
- `MEMORY.md` or `USER.md` nearing limits

Do not run it as a noisy ritual after every ordinary answer.

## Triage Decision Table

| Signal | Destination |
| --- | --- |
| Stable Kei preference | `USER.md` |
| Always-needed active pointer or high-risk reminder | `MEMORY.md` |
| Larger project/system context | `knowledge/*.md` |
| Decision and rationale | `knowledge/decisions.md` |
| Trusted source trail | `knowledge/sources.md` or project note |
| Yuto behavior lesson/failure pattern | `knowledge/yuto.md` |
| Repeatable procedure proven by real use | `/Users/kei/.hermes/skills/` |
| Old conversation detail | `session_search`, not active memory |

## Retrieval Before Action

Before claims or plans, choose the right retrieval path:

- current machine/project state -> command, file read, logs, tests
- project history or policy -> `knowledge/index.md` then relevant note
- old conversation detail -> `session_search`
- recurring procedure -> load relevant skill
- current external fact -> current source lookup

## Maintenance Loop

1. Act on real work.
2. Verify outcome with tools or sources.
3. Distill only durable lessons.
4. Route each lesson to the smallest correct store.
5. Update focused skills when procedure changed or repeated failure occurred.
6. Run canaries only after maintenance or repeated drift.
7. Report only meaningful changes, residual risk, or needed Kei approval.

## Anti-Patterns

- Increasing memory limits instead of routing knowledge.
- Turning every preference into a global rule.
- Creating skills before any real workflow exists.
- Saving task logs or completed-work summaries into active memory.
- Rewriting identity/security architecture casually.
- Reporting maintenance busywork when nothing changed.
