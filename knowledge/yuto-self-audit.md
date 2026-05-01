# Yuto Lightweight Self-Audit

Purpose: keep Yuto's improvement loop observable without turning maintenance into bureaucracy.

Related: [[maintenance]] [[memory-system]] [[yuto]]

## Use When

Use this after maintenance changes, repeated verification drift, or any task where Yuto edited operating/memory files.

Do not use it as a long ritual for every ordinary answer.

## The 4 Canaries

1. Local state claim
   - A current machine-state claim must be backed by a command.
   - Example: model inventory requires `ollama list`.

2. File-specific claim
   - A claim about a current file must be backed by reading that file.

3. Existing-file edit
   - Read the target before editing.
   - Verify the changed section after editing.
   - If a sibling/edit warning appears, stop and re-read before continuing.

4. Explicit consultation
   - If Kei asks Yuto to call an available agent, skill, or tool, do it unless unsafe or unavailable.
   - If unavailable, say why and offer the closest fallback.

## Failure Counters

Keep counters short in [[yuto]].

Rules:
- increment only after a confirmed real failure
- keep one short evidence phrase
- if a counter reaches 2, patch the relevant skill or ask Codex for the smallest repair
- reset/archive after canaries pass consistently

## Output

For maintenance tasks, report:
- files changed
- verification performed
- canary status
- residual risk
