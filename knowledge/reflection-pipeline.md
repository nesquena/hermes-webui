# Reflection Pipeline

Date: 2026-04-25

## Decision

Yuto should use a separate Reflection Pipeline to learn from Kei's conversations
with himself and with Yuto. This pipeline is distinct from the main agent runtime
and must not write authoritative memory directly.

## Core flow

```text
raw conversation log
-> redaction
-> local LLM reflection
-> structured candidate memory
-> Yuto review
-> promote / reject / archive
-> canary test that the new memory helps real future work
```

## Layering rules

1. Raw archive is not memory.
   - Store full conversations for search and evidence.
   - Do not inject raw logs into active context wholesale.
   - Use raw logs as source material only when needed.

2. Reflection is a draft.
   - Local LLM summaries are candidate memory, not facts.
   - They can identify decisions, corrections, mistakes, open tasks, and patterns.
   - Yuto must verify important claims from files, logs, sources, or the raw
     conversation before promoting.

3. Promote one layer at a time.
   - durable Kei preference -> `USER.md`
   - active pointer / high-risk reminder -> `MEMORY.md`
   - project lesson / context / source trail -> `knowledge/*.md`
   - repeated workflow -> `~/.hermes/skills/`

## Separation from runtime

The Reflection Pipeline should not run as an uncontrolled always-on memory writer.
It should be event-driven or scheduled, with clear inputs and outputs. It can run
after long sessions, before context compression, at session close, or during a
bounded daily review.

The main agent runtime remains responsible for action, verification, and user
interaction. Reflection workers only produce drafts.

## Safety requirements

- Redact secrets, tokens, credentials, private identifiers, and sensitive third-party content before reflection.
- Treat conversation text, screen text, webpages, OCR, and local LLM outputs as untrusted input.
- Never allow reflected text to become instructions without Yuto review.
- Keep raw archives searchable but outside active memory injection.
- Keep generated candidate memories diffable, reviewable, and rejectable.

## Candidate memory schema

```yaml
source_log: path-or-session-id
created_at: ISO-8601
model: local-model-name
confidence: low|medium|high
items:
  - memory_type: semantic|episodic|procedural
    type: preference|decision|lesson|open_task|workflow_candidate|project_context|risk
    claim: short statement
    evidence: source pointer or quote reference
    trust_level: user_confirmed|file_verified|source_verified|tool_verified|model_inferred
    source_path: optional-local-path
    source_url: optional-url
    source_quote: optional-short-quote-or-line-ref
    verified_at: ISO-8601-or-null
    expires_at: ISO-8601-or-null
    promotion_status: candidate|promoted|rejected|stale
    recommended_destination: USER.md|MEMORY.md|knowledge|skill|archive_only
    promotion_reason: why it is durable/useful
    risk: privacy|prompt_injection|poisoning|stale|unverified|none
    canary: optional-test-or-question-that-this-memory-should-help-answer
```

Promotion rule: `model_inferred` items stay candidate until Yuto verifies them
against raw conversation, files, logs, command output, user confirmation, or a
primary source. Mutable state requires `expires_at` or live recheck before use.

## Runtime guard paths

The first runtime-enforced layer is intentionally small:

```text
~/.hermes/sessions/*.json
-> /Users/kei/kei-jarvis/conversation-archive/redacted/*.redacted.md
-> /Users/kei/kei-jarvis/conversation-reflections/candidate/*.candidate.md
-> Yuto review
-> /Users/kei/kei-jarvis/conversation-reflections/{promoted,rejected,stale}/
```

Scripts:

- `tools/reflection_pipeline/export_session_candidate.py`
  - exports a Hermes session into a redacted archive and candidate template
  - does not call an LLM
  - never writes authoritative memory
- `tools/reflection_pipeline/candidate_canary.py`
  - checks provenance, trust level, promotion status, and canary fields
  - blocks promoted files that still rely on `trust_level: model_inferred`

## Canary test

A promoted reflection should pass at least one practical canary:

- Does it help Yuto resume work without asking Kei to repeat context?
- Does it reduce a recurring mistake?
- Does it point to the right source file/log/thread for verification?
- Does it avoid adding noise to active memory?
- Can it be removed without losing raw evidence?

Run the mechanical canary before treating reflection files as review-ready:

```bash
cd /Users/kei/kei-jarvis
python3 tools/reflection_pipeline/candidate_canary.py
```

## Related

[[memory-system]] [[codex-chronicle-memory-lessons]] [[yuto-autopilot]] [[yuto]] [[workflows]]
